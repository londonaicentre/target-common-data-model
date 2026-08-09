# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""
Expand UK Core StructureDefinition snapshots into a fully-resolved element tree.

This is stage 1 of the FlatFHIR generator. It applies NO conventions and NO
configuration - it only answers "what elements does this resource actually
have, all the way down". Stage 2 (generate_linkml.py) applies the stage 2
conventions in CONVENTIONS.md and the per-resource config to this output.

Why a separate stage: a profile snapshot stops at datatype boundaries.
`Encounter.period` is a single element of type Period; `period.start` and
`period.end` do not appear anywhere in UKCore-Encounter.json. Likewise a
Reference has no `.reference` child and an extension slice carries only a
profile URL. Every column the CDM names has to be reached by walking into
the datatype and extension definitions in the core package.

Resources are selected by the presence of config/resources/<X>.yaml -
the config file is the opt-in. The config's `profile:` and `package:` keys are
read to locate the StructureDefinition; nothing else in it is consulted.

Everything written here is an opinion-free cache of FHIR (CONVENTIONS.md
"How a resource becomes a schema"). Three artefacts, three jobs:

    build/expanded/<Resource>.yaml   every element, keyed by element path
    build/fhir_bindings.yaml         every bound field, keyed by the field the
                                     binding APPLIES to - not where FHIR
                                     declares it
    build/fhir_enums.yaml            every packaged value set, expanded to its
                                     concepts or the reason it cannot be

None of the three knows anything about the whitelist, and all three record
everything they find.

Usage:
  npm install                        # install FHIR packages once
  uv run scripts/expand_fhir.py
  uv run scripts/expand_fhir.py --resource Encounter
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from valuesets import ValueSetExpander

REPO_ROOT = Path(__file__).parent.parent
CONFIG_DIR = REPO_ROOT / "config" / "resources"
GLOBAL_CONFIG = REPO_ROOT / "config" / "global.yaml"
NODE_MODULES = REPO_ROOT / "node_modules"
OUTPUT_DIR = REPO_ROOT / "build" / "expanded"
BINDINGS_PATH = REPO_ROOT / "build" / "fhir_bindings.yaml"
FHIR_ENUMS_PATH = REPO_ROOT / "build" / "fhir_enums.yaml"

# Types that carry a binding on behalf of the code inside them. FHIR declares
# the binding on the wrapper, but the value that must come from the value set
# sits at the bare `code` beneath it, so the binding is pushed down to that
# field (CONVENTIONS.md "Stage 1 - Bindings").
CONCEPT_TYPES = frozenset({"CodeableConcept", "Coding"})

# Recursion ceiling. FHIR datatypes are mutually recursive - Reference holds an
# Identifier, which holds a Reference (.assigner), and so on without end. The
# expansion is bounded by depth alone; deciding which of these branches the CDM
# keeps is stage 2's job, not this script's.
#
# Expansion mechanics are a property of stage 1 and live here rather than in
# global.yaml, which holds exclusions and nothing else.
DEFAULT_MAX_DEPTH = 6

# Types that are not expanded even though they are complex. Recursing into
# these produces the whole of FHIR and nothing the CDM could use.
OPAQUE_TYPES = frozenset(
    {
        "Resource",
        "DomainResource",
        "Element",
        "BackboneElement",
        "Extension",  # handled separately, via the slice's profile
    }
)

# FHIRPath system types appear in snapshots as the type of `.id` elements and
# of Patient.birthDate.value. They are an artefact of how the spec models
# primitives and never correspond to a column.
FHIRPATH_PREFIX = "http://hl7.org/fhirpath/System."


class ExpansionError(RuntimeError):
    """Raised when a StructureDefinition cannot be located or resolved."""


# ---------------------------------------------------------------------------
# Package access
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _package_index(package: str) -> dict[str, str]:
    """Map canonical URL -> absolute file path for one npm FHIR package."""
    pkg_dir = NODE_MODULES / package
    index_path = pkg_dir / ".index.json"
    if not index_path.exists():
        raise ExpansionError(
            f"{package} not found in node_modules/. Run 'npm install' first."
        )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for entry in index.get("files", []):
        url = entry.get("url")
        if not url:
            continue
        # A package may ship several versions of one canonical; first wins,
        # matching the resolution order npm itself uses.
        out.setdefault(url, str(pkg_dir / entry["filename"]))
    return out


@lru_cache(maxsize=None)
def load_structure_definition(url: str, packages: tuple[str, ...]) -> dict[str, Any]:
    """Fetch a StructureDefinition by canonical URL, searching packages in order.

    The version suffix on a canonical (`...|4.0.1`) is stripped before lookup.
    """
    base = url.split("|", 1)[0]
    for package in packages:
        path = _package_index(package).get(base)
        if path:
            return json.loads(Path(path).read_text(encoding="utf-8"))
    raise ExpansionError(
        f"StructureDefinition not found: {base}\n"
        f"  searched: {', '.join(packages)}"
    )


@lru_cache(maxsize=None)
def datatype_url(type_code: str) -> str:
    """Canonical URL of a base FHIR datatype, e.g. Period -> .../Period."""
    return f"http://hl7.org/fhir/StructureDefinition/{type_code}"


# ---------------------------------------------------------------------------
# Snapshot navigation
# ---------------------------------------------------------------------------


def direct_children(snapshot: list[dict], parent_id: str) -> list[dict]:
    """Elements exactly one level below parent_id in a snapshot.

    Slices count as children of the element they slice, not of the slice's own
    children: `Patient.identifier:nhsNumber` is a child of `Patient`, while
    `Patient.identifier:nhsNumber.value` is a child of the slice.
    """
    out = []
    for el in snapshot:
        eid = el["id"]
        if not eid.startswith(parent_id + "."):
            continue
        remainder = eid[len(parent_id) + 1 :]
        if "." in remainder:
            continue
        out.append(el)
    return out


def element_types(el: dict) -> list[dict]:
    """Type entries, with FHIRPath system types dropped."""
    return [
        t
        for t in el.get("type", [])
        if not str(t.get("code", "")).startswith(FHIRPATH_PREFIX)
    ]


def cardinality(el: dict) -> tuple[int | None, str | None]:
    return el.get("min"), el.get("max")


def is_repeating(el: dict) -> bool:
    return el.get("max") in ("*",) or (
        isinstance(el.get("max"), str) and el["max"].isdigit() and int(el["max"]) > 1
    )


def binding_of(el: dict) -> dict[str, Any] | None:
    b = el.get("binding")
    if not b or not b.get("valueSet"):
        return None
    return {
        "value_set": b["valueSet"].split("|", 1)[0],
        "strength": b.get("strength"),
    }


def short_description(el: dict) -> str:
    return (el.get("short") or el.get("definition") or "").strip()


# ---------------------------------------------------------------------------
# Choice types
# ---------------------------------------------------------------------------


def expand_choice(el: dict) -> list[tuple[str, dict]]:
    """Split a `foo[x]` element into one concrete element per allowed type.

    Condition.onset[x] with types dateTime|Age|Period|Range|string becomes
    onsetDateTime, onsetAge, onsetPeriod, onsetRange, onsetString. Which of
    these the CDM keeps is a stage-2 decision; expansion emits them all.
    """
    base_path = el["id"]
    stem = base_path[: -len("[x]")]
    out = []
    for t in element_types(el):
        code = t["code"]
        # FHIR names the concrete element by title-casing the type code.
        concrete = stem + code[0].upper() + code[1:]
        variant = dict(el)
        variant["id"] = concrete
        variant["path"] = concrete
        variant["type"] = [t]
        out.append((code, variant))
    return out


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


class Expander:
    def __init__(
        self,
        packages: tuple[str, ...],
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        collapse_simple_extensions: bool = True,
    ):
        self.packages = packages
        self.max_depth = max_depth
        self.collapse_simple_extensions = collapse_simple_extensions
        self.records: list[dict] = []
        self.warnings: list[str] = []
        self.current_snapshot: list[dict] = []

    # -- emit -------------------------------------------------------------
    def emit(
        self,
        *,
        path: str,
        fhir_type: str,
        el: dict,
        depth: int,
        origin: str,
        expanded_from: str | None = None,
        target_profiles: list[str] | None = None,
        extension_url: str | None = None,
        repeating_ancestors: list[str],
    ) -> None:
        mn, mx = cardinality(el)
        record: dict[str, Any] = {
            "path": path,
            "fhir_type": fhir_type,
            "min": mn,
            "max": mx,
            "repeating": is_repeating(el),
            "origin": origin,
            "depth": depth,
        }
        if repeating_ancestors:
            record["repeating_ancestors"] = list(repeating_ancestors)
        if expanded_from:
            record["expanded_from"] = expanded_from
        if target_profiles:
            record["target_types"] = [p.rsplit("/", 1)[-1] for p in target_profiles]
            record["target_profiles"] = target_profiles
        if extension_url:
            record["extension_url"] = extension_url
        binding = binding_of(el)
        if binding:
            record["binding"] = binding
        if el.get("sliceName"):
            record["slice_name"] = el["sliceName"]
        if el.get("fixedUri"):
            record["fixed"] = el["fixedUri"]
        desc = short_description(el)
        if desc:
            record["description"] = desc
        self.records.append(record)

    # -- walk -------------------------------------------------------------
    def walk(
        self,
        snapshot: list[dict],
        parent_id: str,
        out_path: str,
        depth: int,
        origin: str,
        repeating_ancestors: list[str],
        seen_types: tuple[str, ...],
    ) -> None:
        if depth > self.max_depth:
            self.warnings.append(f"depth limit reached at {out_path}")
            return

        for el in direct_children(snapshot, parent_id):
            eid = el["id"]
            leaf = eid[len(parent_id) + 1 :]

            if leaf.endswith("[x]"):
                for _code, concrete in expand_choice(el):
                    concrete_leaf = concrete["id"][len(parent_id) + 1 :]
                    self.handle_element(
                        concrete,
                        out_path=f"{out_path}.{concrete_leaf}",
                        depth=depth,
                        origin=origin,
                        repeating_ancestors=repeating_ancestors,
                        seen_types=seen_types,
                    )
                continue

            self.handle_element(
                el,
                out_path=f"{out_path}.{leaf}",
                depth=depth,
                origin=origin,
                repeating_ancestors=repeating_ancestors,
                seen_types=seen_types,
            )

    def handle_element(
        self,
        el: dict,
        *,
        out_path: str,
        depth: int,
        origin: str,
        repeating_ancestors: list[str],
        seen_types: tuple[str, ...],
    ) -> None:
        types = element_types(el)
        if not types:
            # Elements whose only type was a FHIRPath system type (`.id`,
            # `birthDate.value`). They are spec machinery, not data.
            return

        if el.get("max") == "0":
            # Profile has forbidden this element outright.
            return

        type_code = types[0]["code"]
        child_repeats = (
            repeating_ancestors + [out_path] if is_repeating(el) else repeating_ancestors
        )

        # --- Extension slices ------------------------------------------
        if type_code == "Extension":
            profiles = types[0].get("profile") or []
            if not el.get("sliceName"):
                # Anonymous extension - no name, type or binding to derive a
                # column from. Recorded so stage 2 can see it was considered.
                self.emit(
                    path=out_path,
                    fhir_type="Extension",
                    el=el,
                    depth=depth,
                    origin=origin,
                    repeating_ancestors=repeating_ancestors,
                )
                return
            # A simple extension collapses onto this same path, so emitting a
            # marker here as well would produce two records for one column.
            # Let the collapsed value be the only record.
            if not (profiles and self.extension_collapses(profiles[0])):
                self.emit(
                    path=out_path,
                    fhir_type="Extension",
                    el=el,
                    depth=depth,
                    origin=origin,
                    extension_url=profiles[0] if profiles else None,
                    repeating_ancestors=repeating_ancestors,
                )
            if profiles:
                self.expand_extension(
                    profiles[0],
                    out_path=out_path,
                    depth=depth + 1,
                    repeating_ancestors=child_repeats,
                    seen_types=seen_types,
                )
            return

        # --- References -------------------------------------------------
        if type_code == "Reference":
            self.emit(
                path=out_path,
                fhir_type="Reference",
                el=el,
                depth=depth,
                origin=origin,
                target_profiles=types[0].get("targetProfile") or [],
                repeating_ancestors=repeating_ancestors,
            )
            # Reference children (.reference, .type, .identifier, .display) are
            # fixed by the spec and globally pruned by the CDM. Expanding them
            # would also open the Reference -> Identifier -> Reference cycle.
            return

        # --- Backbone elements ------------------------------------------
        if type_code == "BackboneElement":
            self.emit(
                path=out_path,
                fhir_type="BackboneElement",
                el=el,
                depth=depth,
                origin=origin,
                repeating_ancestors=repeating_ancestors,
            )
            # Backbone children live in the same snapshot as the parent.
            self.walk(
                self.current_snapshot,
                el["id"],
                out_path,
                depth + 1,
                origin,
                child_repeats,
                seen_types,
            )
            return

        # --- Everything else --------------------------------------------
        self.emit(
            path=out_path,
            fhir_type=type_code,
            el=el,
            depth=depth,
            origin=origin,
            repeating_ancestors=repeating_ancestors,
        )

        if type_code in OPAQUE_TYPES:
            return

        sd = self.try_load(datatype_url(type_code))
        if sd is None or sd.get("kind") not in ("complex-type",):
            # Primitive, or a type we cannot resolve - nothing below it.
            return

        # Children of this element may already be constrained in the profile
        # snapshot (e.g. Patient.identifier:nhsNumber.system). Prefer those.
        profile_children = direct_children(self.current_snapshot, el["id"])
        if profile_children:
            self.walk(
                self.current_snapshot,
                el["id"],
                out_path,
                depth + 1,
                origin,
                child_repeats,
                seen_types,
            )
            return

        if type_code in seen_types:
            # Same datatype nested inside itself - stop rather than loop.
            return

        saved = self.current_snapshot
        self.current_snapshot = sd["snapshot"]["element"]
        self.walk(
            self.current_snapshot,
            type_code,
            out_path,
            depth + 1,
            f"datatype:{type_code}",
            child_repeats,
            seen_types + (type_code,),
        )
        self.current_snapshot = saved

    # -- extensions -------------------------------------------------------
    def extension_collapses(self, url: str) -> bool:
        """True if this extension definition reduces to a single value column.

        Simple extension - one `value[x]` narrowed to one type, no
        sub-extensions. See global.yaml block 7.
        """
        if not self.collapse_simple_extensions:
            return False
        sd = self.try_load(url)
        if sd is None:
            return False
        snapshot = sd["snapshot"]["element"]
        values = [
            el
            for el in direct_children(snapshot, "Extension")
            if el["id"].endswith(".value[x]") and el.get("max") != "0"
        ]
        subs = [
            el
            for el in direct_children(snapshot, "Extension")
            if el.get("sliceName") and el["id"].split(".")[-1].startswith("extension")
        ]
        return len(values) == 1 and not subs and len(element_types(values[0])) == 1

    def expand_extension(
        self,
        url: str,
        *,
        out_path: str,
        depth: int,
        repeating_ancestors: list[str],
        seen_types: tuple[str, ...],
    ) -> None:
        """Resolve an extension definition to the element(s) that carry its value.

        A simple extension has a single `value[x]` and collapses to one column.
        A complex extension (deathNotificationStatus) has sub-extensions
        instead, each with its own value - so it yields several.
        """
        sd = self.try_load(url)
        if sd is None:
            self.warnings.append(f"unresolved extension {url} at {out_path}")
            return

        snapshot = sd["snapshot"]["element"]
        saved = self.current_snapshot
        self.current_snapshot = snapshot

        value_els = [
            el
            for el in direct_children(snapshot, "Extension")
            if el["id"].endswith(".value[x]") and el.get("max") != "0"
        ]
        sub_exts = [
            el
            for el in direct_children(snapshot, "Extension")
            if el.get("sliceName") and el["id"].split(".")[-1].startswith("extension")
        ]

        for el in value_els:
            # A simple extension - a single value[x] narrowed to one type -
            # collapses onto the extension's own path, so
            # `extension:ethnicCategory` is one column rather than
            # `...ethnicCategory.valueCodeableConcept` (global.yaml block 7).
            collapses = (
                self.collapse_simple_extensions
                and len(value_els) == 1
                and len(element_types(el)) == 1
            )
            for code, concrete in expand_choice(el):
                suffix = "" if collapses else f".value{code[0].upper() + code[1:]}"
                self.handle_extension_value(
                    concrete,
                    out_path=out_path + suffix,
                    depth=depth,
                    repeating_ancestors=repeating_ancestors,
                    seen_types=seen_types,
                    extension_url=url if collapses else None,
                )

        for el in sub_exts:
            slice_name = el["sliceName"]
            self.expand_extension_inline(
                el,
                out_path=f"{out_path}.extension:{slice_name}",
                depth=depth + 1,
                repeating_ancestors=repeating_ancestors,
                seen_types=seen_types,
            )

        self.current_snapshot = saved

    def expand_extension_inline(
        self,
        el: dict,
        *,
        out_path: str,
        depth: int,
        repeating_ancestors: list[str],
        seen_types: tuple[str, ...],
    ) -> None:
        """A sub-extension defined inline inside a complex extension."""
        values = [
            child
            for child in direct_children(self.current_snapshot, el["id"])
            if child["id"].endswith(".value[x]") and child.get("max") != "0"
        ]
        # As at the top level, a sub-extension with one single-typed value
        # collapses onto its own path, so no separate marker is emitted.
        collapses = (
            self.collapse_simple_extensions
            and len(values) == 1
            and len(element_types(values[0])) == 1
        )
        if not collapses:
            self.emit(
                path=out_path,
                fhir_type="Extension",
                el=el,
                depth=depth,
                origin="extension",
                repeating_ancestors=repeating_ancestors,
            )
        child_repeats = (
            repeating_ancestors + [out_path] if is_repeating(el) else repeating_ancestors
        )
        for child in values:
            for code, concrete in expand_choice(child):
                suffix = "" if collapses else f".value{code[0].upper() + code[1:]}"
                self.handle_extension_value(
                    concrete,
                    out_path=out_path + suffix,
                    depth=depth,
                    repeating_ancestors=child_repeats,
                    seen_types=seen_types,
                )

    def handle_extension_value(
        self,
        el: dict,
        *,
        out_path: str,
        depth: int,
        repeating_ancestors: list[str],
        seen_types: tuple[str, ...],
        extension_url: str | None = None,
    ) -> None:
        types = element_types(el)
        if not types:
            return
        type_code = types[0]["code"]
        self.emit(
            path=out_path,
            fhir_type=type_code,
            el=el,
            depth=depth,
            origin="extension",
            target_profiles=types[0].get("targetProfile") or [],
            extension_url=extension_url,
            repeating_ancestors=repeating_ancestors,
        )
        # An extension valued as a datatype (Address on birthPlace) still needs
        # expanding so stage 2 can see what is underneath it.
        if type_code in OPAQUE_TYPES or type_code == "Reference":
            return
        sd = self.try_load(datatype_url(type_code))
        if sd is None or sd.get("kind") != "complex-type":
            return
        if type_code in seen_types:
            return
        saved = self.current_snapshot
        self.current_snapshot = sd["snapshot"]["element"]
        self.walk(
            self.current_snapshot,
            type_code,
            out_path,
            depth + 1,
            f"datatype:{type_code}",
            repeating_ancestors,
            seen_types + (type_code,),
        )
        self.current_snapshot = saved

    # -- helpers ----------------------------------------------------------
    def try_load(self, url: str) -> dict[str, Any] | None:
        try:
            sd = load_structure_definition(url, self.packages)
        except ExpansionError:
            return None
        if "snapshot" not in sd:
            self.warnings.append(f"no snapshot in {url}")
            return None
        return sd

    # -- entry point ------------------------------------------------------
    def expand_resource(self, sd: dict) -> list[dict]:
        self.records = []
        snapshot = sd["snapshot"]["element"]
        self.current_snapshot = snapshot
        root = sd["type"]
        self.walk(snapshot, root, root, 0, "profile", [], ())
        return self.records


# ---------------------------------------------------------------------------
# Binding anchors (CONVENTIONS.md "Stage 1 - Bindings")
# ---------------------------------------------------------------------------


def binding_anchor(path: str, by_path: dict[str, dict]) -> str | None:
    """The field a binding declared on `path` applies to.

    A value set constrains a code, and an object is not a code, so a binding
    on a CodeableConcept or Coding is pushed down to the bare `code` beneath
    it. A binding already on a primitive stays where it stands.

        Condition.clinicalStatus  -> Condition.clinicalStatus.coding.code
        Patient.gender            -> Patient.gender

    Returns None where the wrapper's `code` is not in the tree at all, which
    means no field can carry the binding.
    """
    el = by_path.get(path)
    while el is not None and el.get("fhir_type") in CONCEPT_TYPES:
        child = "code" if el["fhir_type"] == "Coding" else "coding"
        nxt = by_path.get(f"{path}.{child}")
        if nxt is None:
            return None
        path, el = f"{path}.{child}", nxt
    return path if el is not None else None


def resolve_bindings(resource: str, elements: list[dict]) -> list[dict]:
    """Every binding in one resource, keyed by the field it applies to.

    Records everything found, whether or not any config names the field - this
    is a cache of FHIR, and the whitelist does not reach into it. Where several
    declarations push down onto the same field, the deepest declaration wins:
    it is the most specific statement about that code.
    """
    by_path = {el["path"]: el for el in elements}
    out: dict[str, dict[str, Any]] = {}

    for el in elements:
        binding = el.get("binding")
        if not binding:
            continue
        anchor = binding_anchor(el["path"], by_path)
        if anchor is None:
            continue
        prior = out.get(anchor)
        if prior is not None and prior["declared_at"].count(".") >= el["path"].count("."):
            continue
        record: dict[str, Any] = {
            "resource": resource,
            "path": anchor,
            "value_set": binding["value_set"],
            "declared_at": el["path"],
        }
        if binding.get("strength"):
            record["strength"] = binding["strength"]
        out[anchor] = record

    return [out[p] for p in sorted(out)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_bindings(records: list[dict]) -> str:
    header = (
        "# AUTO-GENERATED by scripts/expand_fhir.py - DO NOT EDIT.\n"
        "#\n"
        "# Every binding in every modelled resource, keyed by the field the\n"
        "# binding APPLIES to - the bare `code` - rather than the element FHIR\n"
        "# declares it on. `declared_at` records where FHIR put it.\n"
        "#\n"
        "# An opinion-free cache: every binding found is recorded, whether or\n"
        "# not a resource config names the field. Which of these the CDM uses\n"
        "# is declared per config (CONVENTIONS.md §12).\n"
        "#\n"
        f"# {len(records)} bound fields.\n"
        "#\n"
        "# Regenerate with:  uv run scripts/expand_fhir.py\n"
    )
    return header + yaml.safe_dump(
        {"bindings": records}, sort_keys=False, width=100, allow_unicode=True
    )


def render_fhir_enums(cache: dict[str, dict[str, Any]]) -> str:
    """Every packaged value set, resolved to concepts or to a reason.

    A terminology cache over the packages, not a model. Which of these become
    enums is decided by the resource configs, in stage 2.
    """
    expandable = sum(1 for e in cache.values() if e.get("expandable"))
    header = (
        "# AUTO-GENERATED by scripts/expand_fhir.py - DO NOT EDIT.\n"
        "#\n"
        "# Offline expansion of every ValueSet in the installed FHIR packages.\n"
        "# A cache of terminology: which of these become enums is decided by\n"
        "# the resource configs, and the result is written to cdm/enums.yaml.\n"
        "#\n"
        "# A value set that cannot be expanded offline (a SNOMED `is-a` filter,\n"
        "# a set spanning several code systems) is recorded with the reason\n"
        "# rather than omitted, so an absent enum is never ambiguous.\n"
        "#\n"
        f"# {expandable} of {len(cache)} value sets expand offline.\n"
        "#\n"
        "# Regenerate with:  uv run scripts/expand_fhir.py\n"
    )
    doc = {"value_sets": {url: cache[url] for url in sorted(cache)}}
    return header + yaml.safe_dump(
        doc, sort_keys=False, width=100, allow_unicode=True
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def configured_resources() -> list[tuple[str, dict]]:
    """Resources with a config file - config presence is the opt-in."""
    out = []
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = cfg.get("resource") or path.stem
        if not cfg.get("profile"):
            print(
                f"ERROR: {path.relative_to(REPO_ROOT)} has no `profile:` key",
                file=sys.stderr,
            )
            sys.exit(1)
        out.append((name, cfg))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--resource", help="expand only this resource")
    ap.add_argument(
        "--out",
        type=Path,
        default=OUTPUT_DIR,
        help=f"output directory (default {OUTPUT_DIR.relative_to(REPO_ROOT)})",
    )
    args = ap.parse_args()

    global_cfg = yaml.safe_load(GLOBAL_CONFIG.read_text(encoding="utf-8")) or {}
    pkgs = global_cfg.get("packages", {})
    packages = tuple(
        p for p in (pkgs.get("profile"), pkgs.get("core")) if p
    )
    if not packages:
        print("ERROR: global.yaml declares no packages", file=sys.stderr)
        sys.exit(1)

    resources = configured_resources()
    if args.resource:
        resources = [(n, c) for n, c in resources if n == args.resource]
        if not resources:
            print(f"ERROR: no config for {args.resource}", file=sys.stderr)
            sys.exit(1)

    args.out.mkdir(parents=True, exist_ok=True)
    exit_code = 0
    binding_records: list[dict] = []

    for name, cfg in resources:
        # A resource config may name its own package; fall back to the global
        # profile package.
        res_packages = packages
        if cfg.get("package") and cfg["package"] not in packages:
            res_packages = (cfg["package"],) + packages

        expander = Expander(res_packages)
        try:
            sd = load_structure_definition(cfg["profile"], res_packages)
        except ExpansionError as exc:
            print(f"ERROR [{name}]: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if "snapshot" not in sd:
            print(f"ERROR [{name}]: profile has no snapshot", file=sys.stderr)
            exit_code = 1
            continue

        records = expander.expand_resource(sd)
        binding_records.extend(resolve_bindings(name, records))

        doc = {
            "resource": name,
            "profile": cfg["profile"],
            "profile_version": sd.get("version"),
            "fhir_version": sd.get("fhirVersion"),
            "packages": list(res_packages),
            "element_count": len(records),
            "elements": records,
        }

        out_path = args.out / f"{name}.yaml"
        header = (
            "# AUTO-GENERATED by scripts/expand_fhir.py - DO NOT EDIT.\n"
            "#\n"
            "# Fully-resolved FHIR element tree for one resource: the profile\n"
            "# snapshot with datatypes, extensions and choice types expanded.\n"
            "# No CONVENTIONS.md rules and no resource config are applied here.\n"
            f"# Source: {cfg['profile']}\n"
            "#\n"
            "# Regenerate with:  uv run scripts/expand_fhir.py\n"
        )
        out_path.write_text(
            header + yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True),
            encoding="utf-8",
            newline="\n",
        )

        try:
            rel = out_path.relative_to(REPO_ROOT)
        except ValueError:
            rel = out_path  # --out pointed outside the repo
        print(f"{name:<14} {len(records):>4} elements  -> {rel}")
        for w in expander.warnings:
            print(f"  WARNING: {w}", file=sys.stderr)

    # --- the two package-wide caches ------------------------------------
    # Both describe FHIR rather than any one resource, so they are written
    # once, over whatever was expanded in this run.
    if args.resource:
        # A single-resource run would otherwise overwrite the bindings cache
        # with one resource's worth of it.
        print(
            f"\nNote: --resource {args.resource} - "
            f"{BINDINGS_PATH.relative_to(REPO_ROOT)} not rewritten "
            f"(it covers every resource). Run without --resource to refresh.",
            file=sys.stderr,
        )
        sys.exit(exit_code)

    BINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BINDINGS_PATH.write_text(
        render_bindings(binding_records), encoding="utf-8", newline="\n"
    )
    print(
        f"{'bindings':<14} {len(binding_records):>4} bound     "
        f"-> {BINDINGS_PATH.relative_to(REPO_ROOT)}"
    )

    value_sets = ValueSetExpander(packages).expand_all()
    FHIR_ENUMS_PATH.write_text(
        render_fhir_enums(value_sets), encoding="utf-8", newline="\n"
    )
    expandable = sum(1 for e in value_sets.values() if e.get("expandable"))
    print(
        f"{'value sets':<14} {expandable:>4}/{len(value_sets)} expanded "
        f"-> {FHIR_ENUMS_PATH.relative_to(REPO_ROOT)}"
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
