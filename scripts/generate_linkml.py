# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""
Resolve an expanded FHIR element tree into a LinkML schema.

This is stage 2 of the FlatFHIR generator. Stage 1 (expand_fhir.py) answered
"what elements does this resource have". Stage 2 answers "which of them are
fields, and what shape does each take" - it is where every rule in
CONVENTIONS.md lives.

The pipeline, per resource:

    build/expanded/<Resource>.yaml     stage 1 output
              |
              |  prune      global exclusions (CONVENTIONS.md §1)
              |  select     the config whitelist       (§7, §8)
              |  shape      first-entry / variant / _id / is_source (§2, §5, §6)
              |  bind       FHIR bindings + manual overrides   (§12)
              |  render     LinkML classes and slots           (§4)
              v
    cdm/<Resource>.yaml  +  build/fhir_bindings.yaml

Bindings are declared, never inferred (CONVENTIONS.md "How bindings work"). A
config names the field carrying the bare `code` and says where its vocabulary
comes from:

    config/resources/*.yaml   bindings: <field path>: default | <EnumName>
              |
        default             <EnumName>
              |                   |
              v                   v
    build/fhir_bindings.yaml   config/enum_manifest.yaml
      field -> value set URL     HAND-WRITTEN. Manual bindings only.
              |                        |
              v                        |
    build/fhir_enums.yaml              |
      value set URL -> concepts        |
              |                        |
              +-----------+------------+
                          v
                    cdm/enums.yaml     PUBLISHED, only what configs name.

`default` resolves only where the strength is `required` or `extensible` and
the value set expands offline; anything else is an error rather than a silent
fallback to `string`.

Each bound field records where its codes came from as `binding_source`:
`fhir` for a value set the profile binds, `manual` for a hand-written entry
that still describes that value set, and `local` for codes from outside FHIR
that replace the binding. A `local` field carries the URL it displaces as
`replaces_value_set` rather than `value_set`, so the schema never claims
conformance to a vocabulary the column does not use (§12).

Usage:
  uv run scripts/expand_fhir.py         # stage 1 first
  uv run scripts/generate_linkml.py
  uv run scripts/generate_linkml.py --resource Encounter --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from valuesets import Concept, ValueSetExpander

REPO_ROOT = Path(__file__).parent.parent
CONFIG_DIR = REPO_ROOT / "config" / "resources"
GLOBAL_CONFIG = REPO_ROOT / "config" / "global.yaml"
MANIFEST_PATH = REPO_ROOT / "config" / "enum_manifest.yaml"
EXPANDED_DIR = REPO_ROOT / "build" / "expanded"
OUTPUT_DIR = REPO_ROOT / "cdm"
BINDINGS_PATH = REPO_ROOT / "build" / "fhir_bindings.yaml"
FHIR_ENUMS_PATH = REPO_ROOT / "build" / "fhir_enums.yaml"
ENUMS_PATH = OUTPUT_DIR / "enums.yaml"

SCHEMA_BASE_URI = "https://cdm.aicentre.co.uk"

# `linkml:types` is a CURIE, so the `linkml` prefix has to be declared for the
# schema to resolve outside SchemaView (which tolerates the omission, while the
# generators and validator do not). `default_prefix` names the schema's own
# element URIs.
SCHEMA_DEFAULT_PREFIX = "aiccdm"
SCHEMA_PREFIXES = {
    "linkml": "https://w3id.org/linkml/",
    SCHEMA_DEFAULT_PREFIX: f"{SCHEMA_BASE_URI}/",
}

# The literal a config writes to take the FHIR binding recorded for a field.
DEFAULT_BINDING = "default"

# Strengths under which a source system is obliged to use the value set, and
# so the only ones `default` will resolve (CONVENTIONS.md "How bindings work").
# A `preferred` vocabulary the CDM wants enforced is declared manually (§12).
ENUMERABLE_STRENGTHS = frozenset({"required", "extensible"})


class ResolutionError(RuntimeError):
    """A config names something the expanded tree does not contain."""


# ---------------------------------------------------------------------------
# FHIR primitive -> LinkML range
#
# Anything absent from this table is a complex type, which never reaches
# `linkml_range` - it becomes an inline class instead.
# ---------------------------------------------------------------------------

PRIMITIVE_RANGES: dict[str, str] = {
    "boolean": "boolean",
    "integer": "integer",
    "positiveInt": "integer",
    "unsignedInt": "integer",
    "decimal": "float",
    "string": "string",
    "markdown": "string",
    "code": "string",
    "id": "string",
    "uri": "uri",
    "url": "uri",
    "canonical": "uri",
    "oid": "uri",
    "uuid": "uri",
    "base64Binary": "string",
    "date": "date",
    "dateTime": "datetime",
    "instant": "datetime",
    "time": "time",
}


def linkml_range(fhir_type: str) -> str:
    return PRIMITIVE_RANGES.get(fhir_type, "string")


def is_primitive(fhir_type: str) -> bool:
    return fhir_type in PRIMITIVE_RANGES


# ---------------------------------------------------------------------------
# Paths
#
# A FHIR path here is the stage-1 `path` string: dot-separated segments where a
# slice is written `identifier:nhsNumber`. A config may additionally write an
# explicit `[0]` on a segment (§"How the whitelist works").
# ---------------------------------------------------------------------------

INDEX_SUFFIX = re.compile(r"\[0\]$")


def strip_indices(path: str) -> str:
    """Config path -> stage-1 path. `Patient.name.given[0]` -> `Patient.name.given`."""
    return ".".join(INDEX_SUFFIX.sub("", seg) for seg in path.split("."))


def indexed_segments(path: str) -> set[str]:
    """Prefixes a config marked `[0]`, as stage-1 paths.

    `Encounter.type.coding[0]` -> {"Encounter.type.coding"}. Used only to check
    the author's assertion against the real cardinality.
    """
    out: set[str] = set()
    prefix: list[str] = []
    for seg in path.split("."):
        prefix.append(INDEX_SUFFIX.sub("", seg))
        if INDEX_SUFFIX.search(seg):
            out.add(".".join(prefix))
    return out


def slot_name(path: str, resource: str) -> str:
    """§4. Drop the resource prefix, replace `.` and `:` with `_`, lowercase."""
    rest = path[len(resource) + 1 :] if path.startswith(resource + ".") else path
    return strip_indices(rest).replace(".", "_").replace(":", "_").lower()


def child_name(path: str, parent_path: str) -> str:
    """A name for an element *inside* a field, relative to that field (§4).

    Contents keep their FHIR names, so this is the path below the whitelisted
    entry with the same separator treatment.
    """
    rest = path[len(parent_path) + 1 :]
    return rest.replace(".", "_").replace(":", "_").lower()


def class_name(resource: str, path: str) -> str:
    """CamelCase class name for a variant or inline object."""
    tail = strip_indices(path[len(resource) + 1 :]) if path.startswith(resource + ".") else path
    parts = re.split(r"[.:]", tail)
    return resource + "".join(p[:1].upper() + p[1:] for p in parts if p)


# ---------------------------------------------------------------------------
# Global exclusions (§1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlobalRules:
    exclude_paths: tuple[str, ...]
    exclude_types: frozenset[str]
    exclude_element_id: bool
    exclude_anonymous_extensions: bool
    exclude_coding_children: frozenset[str]
    exclude_reference_children: frozenset[str]

    @classmethod
    def load(cls, cfg: dict) -> "GlobalRules":
        return cls(
            exclude_paths=tuple(cfg.get("exclude_paths") or ()),
            exclude_types=frozenset(cfg.get("exclude_types") or ()),
            exclude_element_id=bool(cfg.get("exclude_element_id", True)),
            exclude_anonymous_extensions=bool(
                cfg.get("exclude_anonymous_extensions", True)
            ),
            exclude_coding_children=frozenset(cfg.get("exclude_coding_children") or ()),
            exclude_reference_children=frozenset(
                cfg.get("exclude_reference_children") or ()
            ),
        )


def prune(elements: list[dict], resource: str, rules: GlobalRules) -> list[dict]:
    """Drop globally excluded elements, and everything beneath them.

    Runs before the whitelist is consulted; a whitelist entry cannot resurrect
    anything dropped here.
    """
    kept: list[dict] = []
    dropped_prefixes: list[str] = []

    for el in elements:
        path = el["path"]
        if any(
            path == p or path.startswith(p + ".") for p in dropped_prefixes
        ):
            continue

        rel = path[len(resource) + 1 :] if path.startswith(resource + ".") else ""
        segments = rel.split(".") if rel else []
        last = segments[-1] if segments else ""

        # Housekeeping paths, relative to the resource root.
        if any(rel == p or rel.startswith(p + ".") for p in rules.exclude_paths):
            dropped_prefixes.append(path)
            continue

        # Rendered and binary payload datatypes, at any depth.
        if el.get("fhir_type") in rules.exclude_types:
            dropped_prefixes.append(path)
            continue

        # Element-level `.id`. Resource.id is the PK and never appears as a
        # child path, so this cannot touch it.
        if rules.exclude_element_id and last == "id":
            dropped_prefixes.append(path)
            continue

        # Anonymous extension / modifierExtension: a last segment of exactly
        # `extension` is anonymous; `extension:ethnicCategory` is a named slice.
        if rules.exclude_anonymous_extensions and last in (
            "extension",
            "modifierExtension",
        ):
            dropped_prefixes.append(path)
            continue

        kept.append(el)

    # Coding and Reference children are pruned by the type of their PARENT, so
    # they need the surviving set to be indexed first.
    by_path = {el["path"]: el for el in kept}

    def parent_typed(path: str, type_code: str) -> bool:
        parent = path.rsplit(".", 1)[0]
        return by_path.get(parent, {}).get("fhir_type") == type_code

    out = []
    for el in kept:
        last = el["path"].rsplit(".", 1)[-1]
        if last in rules.exclude_coding_children and parent_typed(el["path"], "Coding"):
            continue
        if last in rules.exclude_reference_children and parent_typed(
            el["path"], "Reference"
        ):
            continue
        out.append(el)
    return out


# ---------------------------------------------------------------------------
# The element tree
# ---------------------------------------------------------------------------


class ElementTree:
    """Indexed view over one resource's pruned elements."""

    def __init__(self, resource: str, elements: list[dict]):
        self.resource = resource
        self.elements = elements
        self.by_path: dict[str, dict] = {el["path"]: el for el in elements}

    def get(self, path: str) -> dict | None:
        return self.by_path.get(path)

    def children(self, path: str) -> list[dict]:
        """Elements exactly one level below `path`."""
        out = []
        for el in self.elements:
            p = el["path"]
            if p.startswith(path + ".") and "." not in p[len(path) + 1 :]:
                out.append(el)
        return out

    def repeating_ancestors(self, path: str) -> list[str]:
        el = self.by_path.get(path, {})
        return list(el.get("repeating_ancestors") or ())


# ---------------------------------------------------------------------------
# Bindings (CONVENTIONS.md "How bindings work", §12)
#
# Nothing is inferred here. A config declares which field is bound and where
# its vocabulary comes from; this resolves that declaration against the two
# stage-1 caches, and fails where the declaration cannot be honoured.
# ---------------------------------------------------------------------------


@dataclass
class BindingInfo:
    """A resolved binding for one field."""

    enum_name: str
    value_set: str | None
    strength: str | None
    # Where the codes came from, not where they are written down:
    #   fhir    derived from the value set the profile binds (`default`)
    #   manual  hand-written in the manifest, but still describing that value
    #           set - a verbatim copy, a subset, or a union across systems
    #   local   hand-written codes from outside FHIR entirely, replacing the
    #           binding rather than describing it (§12)
    source: str


def derived_enum_name(value_set_url: str) -> str:
    """A stable LinkML enum name for a FHIR value set.

    `.../ValueSet/UKCore-EthnicCategory` -> `UKCoreEthnicCategoryEnum`
    `.../ValueSet/condition-clinical`    -> `ConditionClinicalEnum`
    """
    slug = value_set_url.split("|", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    parts = [p for p in re.split(r"[-_.]", slug) if p]
    stem = "".join(p[:1].upper() + p[1:] for p in parts)
    return f"{stem}Enum"


class BindingResolver:
    """Resolves declared bindings against the stage-1 caches.

    Two sources, chosen by what the config wrote against the field:

        default      build/fhir_bindings.yaml -> build/fhir_enums.yaml
        <EnumName>   config/enum_manifest.yaml
    """

    def __init__(
        self,
        manifest: dict[str, dict],
        expander: ValueSetExpander,
        fhir_bindings: dict[str, dict[str, dict]],
        fhir_enums: dict[str, dict[str, Any]],
    ):
        self.manifest = manifest
        self.expander = expander
        # {resource: {field path: {value_set, strength, ...}}}, from stage 1.
        self.fhir_bindings = fhir_bindings
        # {value set URL: {expandable, concepts, note}}, from stage 1.
        self.fhir_enums = fhir_enums
        self.warnings: list[str] = []
        # Enums derived through `default`, for cdm/enums.yaml to pick up.
        self.derived: dict[str, dict[str, Any]] = {}
        # Manual enums a config actually names, so cdm/enums.yaml carries only
        # what the model references.
        self.used_manual: set[str] = set()
        # Manifest entries whose values match what `default` would have given.
        self.redundant: dict[str, str] = {}
        # `local_codes` entries that turned out to reproduce their value set.
        self.mislabelled: dict[str, str] = {}
        # Bound fields no config declared, reported at the end.
        self.undeclared: list[tuple[str, str, str]] = []

    # -- lookups ----------------------------------------------------------
    def fhir_binding(self, resource: str, path: str) -> dict | None:
        """The stage-1 binding recorded against this exact field."""
        return self.fhir_bindings.get(resource, {}).get(path)

    def _concepts(self, value_set: str) -> tuple[list[Concept] | None, str]:
        entry = self.fhir_enums.get(value_set)
        if entry is None:
            return None, "value set not found in the installed packages"
        if not entry.get("expandable"):
            return None, entry.get("note", "not expandable offline")
        return [
            Concept(c["code"], c.get("display", ""), entry.get("system", ""))
            for c in entry.get("concepts", [])
        ], entry.get("note", "")

    def manifest_codes(self, name: str) -> set[str] | None:
        """Codes of a manual enum, where they can be determined."""
        spec = self.manifest.get(name) or {}
        if spec.get("source") == "manual":
            return {str(v["code"]) for v in (spec.get("values") or [])}
        if spec.get("source") == "codesystem" and spec.get("url"):
            cs = self.expander.load("CodeSystem", spec["url"])
            if cs and cs.get("content") == "complete":
                return {
                    c.code
                    for c in ValueSetExpander.flatten_codesystem(cs.get("concept") or [])
                }
        return None

    # -- resolution -------------------------------------------------------
    def resolve(self, resource: str, path: str, declared: str) -> BindingInfo:
        """Resolve one declared binding, or raise.

        `declared` is what the config wrote: `default`, or a manifest entry
        name. Anything that cannot be honoured is an error - a declaration the
        generator cannot satisfy is a mistake in the config, not a field that
        quietly becomes a string.
        """
        fhir = self.fhir_binding(resource, path)

        if declared != DEFAULT_BINDING:
            return self._resolve_manual(resource, path, declared, fhir)

        if fhir is None:
            raise ResolutionError(
                f"{resource}: `default` on {path}, which carries no FHIR "
                f"binding. Name a manifest entry, or drop the declaration."
            )

        strength = fhir.get("strength")
        if strength not in ENUMERABLE_STRENGTHS:
            raise ResolutionError(
                f"{resource}: `default` on {path} is bound `{strength}`, which "
                f"FHIR does not oblige a source system to use. Declare a "
                f"manifest entry to enforce {fhir['value_set']} anyway (§12)."
            )

        value_set = fhir["value_set"]
        concepts, note = self._concepts(value_set)
        if concepts is None:
            raise ResolutionError(
                f"{resource}: `default` on {path} cannot be resolved - "
                f"{value_set} does not expand offline ({note}). Declare a "
                f"manifest entry, or drop the declaration (§12)."
            )

        enum_name = derived_enum_name(value_set)
        self.derived.setdefault(
            enum_name,
            {
                "description": f"Derived from {value_set} ({note}).",
                "value_set": value_set,
                "strength": strength,
                "values": [{"code": c.code, "display": c.display} for c in concepts],
            },
        )
        return BindingInfo(enum_name, value_set, strength, "fhir")

    def _resolve_manual(
        self, resource: str, path: str, name: str, fhir: dict | None
    ) -> BindingInfo:
        if name not in self.manifest:
            raise ResolutionError(
                f"{resource}: binding `{name}` on {path} is not in "
                f"{MANIFEST_PATH.relative_to(REPO_ROOT)}"
            )
        self.used_manual.add(name)
        if self.manifest[name].get("local_codes"):
            # Local codes replace the FHIR binding rather than narrowing it, so
            # the field does not carry that value set (§12). Both the URL and
            # its strength are kept: the strength is what says whether the
            # replacement is conformant - `preferred` permits it, `required`
            # would not - so dropping it would hide the one fact a reviewer
            # needs.
            if fhir:
                self._flag_if_not_local(name, fhir, path)
            return BindingInfo(
                name,
                (fhir or {}).get("value_set"),
                (fhir or {}).get("strength"),
                "local",
            )
        if fhir:
            self._flag_if_redundant(name, fhir, path)
        return BindingInfo(
            name,
            (fhir or {}).get("value_set"),
            (fhir or {}).get("strength"),
            "manual",
        )

    def _flag_if_not_local(self, name: str, fhir: dict, path: str) -> None:
        """Report a `local_codes` entry that is really the FHIR value set.

        Unlike the redundancy check this ignores binding strength: an entry
        claiming its codes come from outside FHIR is wrong wherever they
        reproduce the bound value set, reachable by `default` or not (§12).
        """
        if name in self.mislabelled:
            return
        manual = self.manifest_codes(name)
        if manual is None:
            return
        concepts, _ = self._concepts(fhir["value_set"])
        if concepts is None:
            return
        if manual == {c.code for c in concepts}:
            self.mislabelled[name] = fhir["value_set"]
            self.warnings.append(
                f"manual binding `{name}` ({path}) is declared `local_codes` "
                f"but reproduces the FHIR value set {fhir['value_set']} it "
                f"claims to replace - drop the flag (§12)"
            )

    def _flag_if_redundant(self, name: str, fhir: dict, path: str) -> None:
        """Report a manual enum that reproduces what `default` would give.

        Only where `default` would actually have resolved: a manifest entry
        covering a `preferred` binding is doing real work even if its codes
        happen to match, because `default` could not have reached it (§12).
        """
        if name in self.redundant:
            return
        if fhir.get("strength") not in ENUMERABLE_STRENGTHS:
            return
        manual = self.manifest_codes(name)
        if manual is None:
            return
        concepts, _ = self._concepts(fhir["value_set"])
        if concepts is None:
            return
        if manual == {c.code for c in concepts}:
            self.redundant[name] = fhir["value_set"]
            self.warnings.append(
                f"manual binding `{name}` ({path}) is identical to its FHIR "
                f"value set {fhir['value_set']}, which `default` would have "
                f"resolved - the manifest entry is redundant (§12)"
            )

    def note_undeclared(self, resource: str, path: str) -> None:
        """A whitelisted field carries a FHIR binding no config declared.

        Reported, never acted on: the field is emitted as a plain field
        (CONVENTIONS.md "Post-generation checks").
        """
        fhir = self.fhir_binding(resource, path)
        if fhir:
            self.undeclared.append((resource, path, fhir["value_set"]))


# ---------------------------------------------------------------------------
# Resolved model
# ---------------------------------------------------------------------------


@dataclass
class Slot:
    name: str
    range: str
    fhir_path: str
    fhir_type: str
    required: bool = False
    multivalued: bool = False
    description: str | None = None
    enum: str | None = None
    binding: BindingInfo | None = None
    fk_target: str | None = None
    inlined_class: str | None = None
    identifier: bool = False


@dataclass
class VariantClass:
    name: str
    fhir_path: str
    description: str | None
    key_fields: list[str] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)


@dataclass
class ResourceSchema:
    resource: str
    profile: str
    profile_version: str | None
    fhir_version: str | None
    description: str | None
    slots: list[Slot] = field(default_factory=list)
    classes: list[VariantClass] = field(default_factory=list)
    enums_used: set[str] = field(default_factory=set)
    fk_targets: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Resolution (§2, §5, §6, §7, §8, §9, §10)
# ---------------------------------------------------------------------------


class EntrySpec:
    """One whitelist entry, parsed and validated against the expanded tree.

    A config entry carries the path itself plus four configurables (§7):
    `exclude` (§8), `key` (§9), `fk` (§10) and `bindings` (§12). Reading them
    is validation, not emission - nothing here touches the schema. What comes
    out is the per-entry context the emitters need, held in one place rather
    than threaded through their signatures.

    `used` accumulates the binding paths that reached a field, so the caller
    can report any declaration that never landed.
    """

    def __init__(self, entry: str, opts: dict, el: dict, owner: "Resolver"):
        self.entry = entry
        self.opts = opts
        self.el = el
        self.path = strip_indices(entry)
        self.resource = owner.resource

        self._tree = owner.tree
        self._warn = owner.warnings.append
        self._fail = owner._fail

        self._check_indices()
        self.excludes = self._read_excludes()
        self.fk = opts.get("fk")
        self.bindings = self._read_bindings()
        self.key_fields = list(opts.get("key") or ())
        self.used: set[str] = set()

    # -- validation -------------------------------------------------------
    def _check_indices(self) -> None:
        """`[0]` asserts an array; warn when it is written on a scalar."""
        for prefix in indexed_segments(self.entry):
            el = self._tree.get(prefix)
            if el is not None and not el.get("repeating"):
                self._warn(
                    f"`{self.entry}` writes [0] on `{prefix}`, which is not 0..*"
                )

    def _read_excludes(self) -> set[str]:
        """§8. Child paths that should not appear beneath this entry."""
        out: set[str] = set()
        for raw in (self.opts.get("exclude") or {}):
            ex = strip_indices(raw)
            if not (ex == self.path or ex.startswith(self.path + ".")):
                self._warn(
                    f"exclude `{raw}` on `{self.entry}` is not beneath it; ignored"
                )
                continue
            if self._tree.get(ex) is None:
                self._warn(
                    f"exclude `{raw}` on `{self.entry}` is not in the expanded tree"
                )
            out.add(ex)
        return out

    def _read_bindings(self) -> dict[str, str]:
        """§12. Declared bindings for this entry, keyed by absolute FHIR path.

        Every key is written in full and must sit at or beneath the entry. No
        anchor walking happens here: the path the author wrote is the path that
        is bound, because stage 1 already recorded each binding against the
        field it applies to.
        """
        declared = self.opts.get("bindings") or {}
        if not isinstance(declared, dict):
            self._fail(
                f"{self.resource}: `bindings` on `{self.entry}` must be a mapping of "
                f"field path to `default` or a manifest entry name (§12)"
            )
            return {}

        out: dict[str, str] = {}
        for raw, value in declared.items():
            path = strip_indices(str(raw))
            if not (path == self.path or path.startswith(self.path + ".")):
                self._fail(
                    f"{self.resource}: binding path `{raw}` is not beneath "
                    f"`{self.entry}` (§12)"
                )
                continue
            if self._tree.get(path) is None:
                self._fail(
                    f"{self.resource}: binding path `{raw}` is not in the "
                    f"expanded tree (or was globally excluded)"
                )
                continue
            out[path] = str(value)
        return out

    # -- queries used during emission -------------------------------------
    @property
    def is_variant(self) -> bool:
        """§5. A whitelisted path that repeats is a variant, unless the author
        named `[0]` on its last segment to take the first entry alone."""
        return bool(self.el.get("repeating")) and not INDEX_SUFFIX.search(
            self.entry.split(".")[-1]
        )

    def fk_for(self, path: str) -> str | None:
        """§10. `fk: Patient` binds the entry; a mapping binds paths beneath it."""
        if self.fk is None:
            return None
        if isinstance(self.fk, str):
            return self.fk if path == self.path else None
        return self.fk.get(path) or self.fk.get(f"{path}[0]")

    def excluded(self, path: str) -> bool:
        return any(path == ex or path.startswith(ex + ".") for ex in self.excludes)

    def unused_bindings(self) -> list[str]:
        """Declared binding paths that never reached a field the schema emits."""
        return sorted(set(self.bindings) - self.used)


class ClassBuilder:
    """Builds the inline objects behind a complex or repeating entry (§4, §5).

    Everything under a path becomes the object's fields, keeping their FHIR
    names. `build` and `_add_member` are mutually recursive: a complex child
    becomes a nested object, which may itself hold one. Depth is bounded by
    the tree beneath the whitelisted path, not by a rule here.

    Class naming is owned here, so the counter that disambiguates repeated
    names lives with the code that creates them.
    """

    def __init__(self, owner: "Resolver"):
        self._resolver = owner
        self._tree = owner.tree
        self.resource = owner.resource
        self._class_counter: dict[str, int] = {}

    def build(
        self,
        spec: EntrySpec,
        path: str,
        el: dict,
        schema: ResourceSchema,
        *,
        key_fields: list[str],
    ) -> VariantClass | None:
        """Everything under `path` becomes the object's fields (§4).

        Contents keep their FHIR names. A repeating element inside the object
        is itself a nested variant; a Reference inside it is an `_id` (§2).
        """
        cls = VariantClass(
            name=self._unique_class_name(path),
            fhir_path=path,
            description=el.get("description"),
            key_fields=key_fields,
        )

        for child in self._members(spec, path):
            self._add_member(cls, child, path, spec, schema)

        # §6. Every Coding that survives carries an is_source flag.
        if el["fhir_type"] == "Coding":
            cls.slots.append(
                Slot(
                    name="is_source",
                    range="boolean",
                    fhir_path=f"{path}.is_source",
                    fhir_type="boolean",
                    description=(
                        "True on the coding that came from the source system; "
                        "false on codings added by a downstream mapping step. "
                        "CDM-defined, not native to FHIR (§6)."
                    ),
                )
            )

        return cls if cls.slots else None

    def _members(self, spec: EntrySpec, path: str) -> list[dict]:
        """Direct children of `path` that survive this entry's exclusions."""
        return [c for c in self._tree.children(path) if not spec.excluded(c["path"])]

    def _add_member(
        self,
        cls: VariantClass,
        child: dict,
        parent_path: str,
        spec: EntrySpec,
        schema: ResourceSchema,
    ) -> None:
        cpath = child["path"]
        ctype = child["fhir_type"]
        cname = child_name(cpath, parent_path)

        # §2. A Reference at any depth is an `_id`.
        if ctype == "Reference":
            target = spec.fk_for(cpath) or Resolver._sole_target(child)
            if target:
                schema.fk_targets.add(target)
            cls.slots.append(
                Slot(
                    name=f"{cname}_id",
                    range=(
                        target if target in self._resolver.modelled else "string"
                    ),
                    fhir_path=cpath,
                    fhir_type="Reference",
                    required=bool(child.get("min")),
                    multivalued=bool(child.get("repeating")),
                    description=child.get("description"),
                    fk_target=target,
                )
            )
            return

        # Only a value can carry an enum; a binding declared on a complex
        # child is left unconsumed and reported by the caller.
        if is_primitive(ctype):
            binding = self._resolver.binding_for(cpath, spec)
            if binding:
                schema.enums_used.add(binding.enum_name)
            cls.slots.append(
                Slot(
                    name=cname,
                    range=binding.enum_name if binding else linkml_range(ctype),
                    fhir_path=cpath,
                    fhir_type=ctype,
                    required=bool(child.get("min")),
                    multivalued=bool(child.get("repeating")),
                    description=child.get("description"),
                    enum=binding.enum_name if binding else None,
                    binding=binding,
                )
            )
            return

        # A complex child becomes a nested inline object - an array of them
        # where it repeats (§5: a variant may hold a further variant).
        nested = self.build(spec, cpath, child, schema, key_fields=[])
        if nested is None:
            return
        schema.classes.append(nested)
        cls.slots.append(
            Slot(
                name=cname,
                range=nested.name,
                fhir_path=cpath,
                fhir_type=ctype,
                required=bool(child.get("min")),
                multivalued=bool(child.get("repeating")),
                description=child.get("description"),
                inlined_class=nested.name,
            )
        )

    def _unique_class_name(self, path: str) -> str:
        base = class_name(self.resource, path)
        n = self._class_counter.get(base, 0)
        self._class_counter[base] = n + 1
        return base if n == 0 else f"{base}{n + 1}"


class Resolver:
    """Turns a whitelist into a `ResourceSchema`.

    Orchestrates one resource: reads each entry through `EntrySpec`, emits the
    field or variant it resolves to, and delegates inline object construction
    to `ClassBuilder`. It also owns the single bridge to `BindingResolver`
    (`binding_for`) and the strict/warn error policy (`_fail`).
    """

    def __init__(
        self,
        tree: ElementTree,
        cfg: dict,
        bindings: BindingResolver,
        *,
        strict: bool = True,
        modelled: frozenset[str] = frozenset(),
    ):
        self.tree = tree
        self.cfg = cfg
        self.bindings = bindings
        self.resource = tree.resource
        self.strict = strict
        # Resources the CDM models, i.e. those with a config. A Reference to
        # one of these gets that class as its range (§10); a Reference to
        # anything else stays a bare string, as there is no class to point at.
        self.modelled = modelled
        self.warnings: list[str] = []
        self.classes = ClassBuilder(self)

    # -- entry point ------------------------------------------------------
    def resolve(self, doc: dict) -> ResourceSchema:
        schema = ResourceSchema(
            resource=self.resource,
            profile=doc.get("profile", ""),
            profile_version=doc.get("profile_version"),
            fhir_version=doc.get("fhir_version"),
            description=self._resource_description(),
        )
        self._add_provenance(schema)

        include = self.cfg.get("include") or {}
        for entry, opts in include.items():
            opts = opts or {}
            self._resolve_entry(entry, opts, schema)
        return schema

    def _resource_description(self) -> str | None:
        el = self.tree.get(self.resource)
        return (el or {}).get("description")

    # -- §3 primary key and provenance ------------------------------------
    def _add_provenance(self, schema: ResourceSchema) -> None:
        """Every table carries these without being declared."""
        schema.slots.append(
            Slot(
                name="id",
                range="string",
                fhir_path=f"{self.resource}.id",
                fhir_type="id",
                identifier=True,
                description="Logical id of this resource.",
            )
        )
        schema.slots.append(
            Slot(
                name="meta_source",
                range="uri",
                fhir_path=f"{self.resource}.meta.source",
                fhir_type="uri",
                description="Identifies where the resource comes from.",
            )
        )
        schema.slots.append(
            Slot(
                name="meta_last_updated",
                range="datetime",
                fhir_path=f"{self.resource}.meta.lastUpdated",
                fhir_type="instant",
                description="When the resource version last changed.",
            )
        )

    # -- one whitelist entry ----------------------------------------------
    def _resolve_entry(self, entry: str, opts: dict, schema: ResourceSchema) -> None:
        path = strip_indices(entry)
        el = self.tree.get(path)
        if el is None:
            self._fail(
                f"{self.resource}: whitelisted path `{entry}` is not in the "
                f"expanded tree (or was globally excluded)"
            )
            return

        spec = EntrySpec(entry, opts, el, self)

        # §5. A whitelisted path that repeats is a variant; anything else is a
        # single field, taking the first entry of any array above it (§"How the
        # whitelist works").
        if spec.is_variant:
            self._emit_variant(spec, schema)
        else:
            self._emit_field(spec, schema)

        # Every declared binding must land on a field the schema emits (§12).
        for unused in spec.unused_bindings():
            self._fail(
                f"{self.resource}: binding path `{unused}` on `{entry}` is not "
                f"a field of the schema - it was excluded, or it is a wrapper "
                f"rather than the code beneath it (§12)"
            )

    def binding_for(self, path: str, spec: EntrySpec) -> BindingInfo | None:
        """The declared binding for one field, or None if none was declared.

        The single bridge to `BindingResolver` - `ClassBuilder` calls it too,
        so a binding is resolved the same way at any depth.

        A field with no declaration is a plain field, whatever FHIR says about
        it; where FHIR does bind it, that is reported at the end of the run
        (CONVENTIONS.md "Post-generation checks").
        """
        name = spec.bindings.get(path)
        if name is None:
            self.bindings.note_undeclared(self.resource, path)
            return None
        spec.used.add(path)
        try:
            return self.bindings.resolve(self.resource, path, name)
        except ResolutionError as exc:
            self._fail(str(exc))
            return None

    # -- a single (non-repeating) field -----------------------------------
    def _emit_field(self, spec: EntrySpec, schema: ResourceSchema) -> None:
        entry, path, el = spec.entry, spec.path, spec.el
        name = slot_name(entry, self.resource)
        fhir_type = el["fhir_type"]
        required = bool(el.get("min")) and not self.tree.repeating_ancestors(path)

        # §2. A Reference is a scalar `_id` foreign key. Where the target is
        # known and modelled, the range IS the target class - LinkML's native
        # reference, serialised as the target's identifier (§10).
        if fhir_type == "Reference":
            target = spec.fk_for(path) or self._sole_target(el)
            slot = Slot(
                name=f"{name}_id",
                range=target if target in self.modelled else "string",
                fhir_path=path,
                fhir_type="Reference",
                required=required,
                description=el.get("description"),
                fk_target=target,
            )
            if target:
                schema.fk_targets.add(target)
            schema.slots.append(slot)
            return

        # A primitive is a value; anything complex is an inline object built
        # from whatever survives beneath it. Only a value can carry an enum, so
        # a binding declared on a complex path is left unconsumed and reported
        # by the caller.
        if is_primitive(fhir_type):
            binding = self.binding_for(path, spec)
            slot = Slot(
                name=name,
                range=binding.enum_name if binding else linkml_range(fhir_type),
                fhir_path=path,
                fhir_type=fhir_type,
                required=required,
                description=el.get("description"),
                enum=binding.enum_name if binding else None,
                binding=binding,
            )
            if binding:
                schema.enums_used.add(binding.enum_name)
            schema.slots.append(slot)
            return

        cls = self.classes.build(spec, path, el, schema, key_fields=[])
        if cls is None:
            # Nothing survived beneath a complex element - emit nothing rather
            # than an empty object.
            self.warnings.append(f"`{entry}` has no surviving children; skipped")
            return
        schema.classes.append(cls)
        schema.slots.append(
            Slot(
                name=name,
                range=cls.name,
                fhir_path=path,
                fhir_type=fhir_type,
                required=required,
                description=el.get("description"),
                inlined_class=cls.name,
            )
        )

    # -- a variant (§5) ----------------------------------------------------
    def _emit_variant(self, spec: EntrySpec, schema: ResourceSchema) -> None:
        entry, path, el = spec.entry, spec.path, spec.el
        name = slot_name(entry, self.resource)
        fhir_type = el["fhir_type"]

        # A repeating primitive is an array of values, with no object to key.
        if is_primitive(fhir_type):
            binding = self.binding_for(path, spec)
            slot = Slot(
                name=name,
                range=binding.enum_name if binding else linkml_range(fhir_type),
                fhir_path=path,
                fhir_type=fhir_type,
                multivalued=True,
                description=el.get("description"),
                enum=binding.enum_name if binding else None,
                binding=binding,
            )
            if binding:
                schema.enums_used.add(binding.enum_name)
            schema.slots.append(slot)
            return

        # §2. A repeating Reference is an array of `_id` values.
        if fhir_type == "Reference":
            target = spec.fk_for(path) or self._sole_target(el)
            if target:
                schema.fk_targets.add(target)
            schema.slots.append(
                Slot(
                    name=f"{name}_id",
                    range=target if target in self.modelled else "string",
                    fhir_path=path,
                    fhir_type="Reference",
                    multivalued=True,
                    description=el.get("description"),
                    fk_target=target,
                )
            )
            return

        # §9. Every variant that is an array of objects declares a key. A
        # repeating primitive or Reference resolves to an array of scalars
        # above, where there is no object to identify.
        if not spec.key_fields:
            self._fail(f"{self.resource}: variant `{entry}` declares no `key` (§9)")

        cls = self.classes.build(spec, path, el, schema, key_fields=spec.key_fields)
        if cls is None:
            self.warnings.append(f"variant `{entry}` has no surviving children; skipped")
            return

        self._check_keys(entry, cls)
        schema.classes.append(cls)
        schema.slots.append(
            Slot(
                name=name,
                range=cls.name,
                fhir_path=path,
                fhir_type=fhir_type,
                multivalued=True,
                description=el.get("description"),
                inlined_class=cls.name,
            )
        )

    def _check_keys(self, entry: str, cls: VariantClass) -> None:
        available = {s.name for s in cls.slots}
        for k in cls.key_fields:
            if k not in available:
                self._fail(
                    f"{self.resource}: key `{k}` on variant `{entry}` is not a "
                    f"field of the object (have: {', '.join(sorted(available))})"
                )

    @staticmethod
    def _sole_target(el: dict) -> str | None:
        """Where a Reference permits exactly one target, no `fk` is needed."""
        targets = el.get("target_types") or []
        return targets[0] if len(targets) == 1 else None

    def _fail(self, message: str) -> None:
        if self.strict:
            raise ResolutionError(message)
        self.warnings.append(message)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_slot(slot: Slot) -> dict[str, Any]:
    out: dict[str, Any] = {"range": slot.range}
    if slot.identifier:
        # §3. `identifier` implies required and globally unique, so `required`
        # is not written alongside it.
        out["identifier"] = True
    elif slot.required:
        out["required"] = True
    if slot.multivalued:
        out["multivalued"] = True
    if slot.multivalued and slot.inlined_class:
        # An array of objects is carried inline; an array of references is not.
        out["inlined_as_list"] = True
    if slot.fk_target and slot.range == slot.fk_target:
        # §10. The range is the target class, so the slot is a reference,
        # serialised as that class's identifier rather than an inlined object.
        out["inlined"] = False
    if slot.description:
        out["description"] = slot.description

    ann: dict[str, Any] = {"fhir_path": slot.fhir_path, "fhir_type": slot.fhir_type}
    if slot.fk_target:
        ann["fk_target"] = slot.fk_target
    if slot.binding:
        if slot.binding.value_set:
            # Local codes are not the bound value set, so the URL is recorded
            # as what the field replaces rather than what it conforms to (§12).
            # `binding_strength` still follows, qualifying the replaced binding.
            key = (
                "replaces_value_set"
                if slot.binding.source == "local"
                else "value_set"
            )
            ann[key] = slot.binding.value_set
        if slot.binding.strength:
            ann["binding_strength"] = slot.binding.strength
        ann["binding_source"] = slot.binding.source
    out["annotations"] = ann
    return out


def render_schema(schema: ResourceSchema, cfg_path: Path) -> str:
    resource = schema.resource
    # Derived from the slots actually emitted rather than from the declared
    # fk targets: only a reference whose range became the target class needs
    # that class in scope, and a target the CDM does not model never does.
    referenced = {
        s.range
        for s in schema.slots + [s for c in schema.classes for s in c.slots]
        if s.fk_target and s.range == s.fk_target and s.range != resource
    }
    doc: dict[str, Any] = {
        "id": f"{SCHEMA_BASE_URI}/{resource.lower()}",
        "name": resource.lower(),
        "description": (
            f"Profile: {schema.profile} (FHIR {schema.fhir_version}). "
            f"Grain: one row per {resource} resource."
        ),
        "annotations": {
            "grain": "one_row_per_resource",
            "generated_from": schema.profile,
            "profile_version": schema.profile_version,
        },
        "prefixes": dict(SCHEMA_PREFIXES),
        "default_prefix": SCHEMA_DEFAULT_PREFIX,
        # §10. A reference slot's range is the target class, which has to be
        # in scope, so each FK target's schema is imported. LinkML permits the
        # mutual imports this produces (Encounter <-> Patient, and the
        # self-import a self-reference such as Encounter.partOf would ask for,
        # which is dropped as a schema cannot import itself).
        "imports": ["linkml:types", "enums"] + sorted(t for t in referenced),
        "default_range": "string",
    }

    classes: dict[str, Any] = {}
    for cls in schema.classes:
        block: dict[str, Any] = {}
        if cls.description:
            block["description"] = cls.description
        ann: dict[str, Any] = {"fhir_path": cls.fhir_path}
        if cls.key_fields:
            # A LinkML annotation value is a scalar, so the key is recorded as
            # a comma-separated list of the object's own field names (§9).
            ann["key_fields"] = ", ".join(cls.key_fields)
        block["annotations"] = ann
        block["attributes"] = {s.name: render_slot(s) for s in cls.slots}
        classes[cls.name] = block

    main: dict[str, Any] = {
        "description": schema.description or f"One row per {resource} resource.",
        # The PK is declared on the `id` slot itself as `identifier: true`
        # (§3), so no annotation restates it here.
        "annotations": {"fhir_path": resource},
        "attributes": {s.name: render_slot(s) for s in schema.slots},
    }
    classes[resource] = main
    doc["classes"] = classes

    header = (
        "# AUTO-GENERATED by scripts/generate_linkml.py - DO NOT EDIT.\n"
        "#\n"
        "# Stage 2 of the FlatFHIR generator: the resource whitelist resolved\n"
        "# against the expanded FHIR element tree, following CONVENTIONS.md.\n"
        f"# Source:  {schema.profile}\n"
        f"# Config:  {cfg_path.relative_to(REPO_ROOT)}\n"
        "#\n"
        "# Regenerate with:  uv run scripts/generate_linkml.py\n"
    )
    return header + yaml.safe_dump(
        doc, sort_keys=False, width=100, allow_unicode=True
    )


def render_enums(
    manifest: dict[str, dict],
    used_manual: set[str],
    derived: dict[str, dict],
    expander: ValueSetExpander,
) -> tuple[str, int]:
    """Fold the two binding sources into the published cdm/enums.yaml.

    The manifest (hand-written) and the expansion intermediate (derived) are
    peers with the same structure; manual overrides FHIR (CONVENTIONS.md
    "How bindings work"). Only enums a config actually references are emitted,
    so the published file matches what the schemas import.
    """
    enums: dict[str, Any] = {}

    # -- manual, from the manifest ---------------------------------------
    for name in sorted(used_manual):
        spec = manifest.get(name) or {}
        source = spec.get("source")
        if source == "manual":
            values = [
                Concept(str(v["code"]), v.get("display", ""))
                for v in (spec.get("values") or [])
            ]
            origin = "enum_manifest.yaml"
        elif source == "codesystem":
            cs = expander.load("CodeSystem", spec.get("url", ""))
            values = (
                ValueSetExpander.flatten_codesystem(cs.get("concept") or [])
                if cs
                else []
            )
            origin = spec.get("url", "")
        else:
            values, origin = [], "unknown"

        ann: dict[str, Any] = {
            "binding_source": "local" if spec.get("local_codes") else "manual",
            "origin": origin,
        }
        enums[name] = {
            "description": spec.get("description", ""),
            "annotations": ann,
            "permissible_values": _permissible(values),
        }

    # -- derived, from the expansion intermediate ------------------------
    for name in sorted(derived):
        if name in enums:
            # A manual binding of the same name wins (§12).
            continue
        spec = derived[name]
        enums[name] = {
            "description": spec["description"],
            "annotations": {
                "binding_source": "fhir",
                "value_set": spec["value_set"],
                "binding_strength": spec["strength"],
            },
            "permissible_values": _permissible(
                [Concept(str(v["code"]), v.get("display", "")) for v in spec["values"]]
            ),
        }

    doc = {
        "id": f"{SCHEMA_BASE_URI}/enums",
        "name": "enums",
        "description": (
            "LinkML enums for the FlatFHIR CDM. Folded from the hand-written "
            "manifest and the offline expansion of bound FHIR value sets; "
            "contains only the enums the resource schemas reference."
        ),
        "prefixes": dict(SCHEMA_PREFIXES),
        "default_prefix": SCHEMA_DEFAULT_PREFIX,
        "imports": ["linkml:types"],
        "enums": enums,
    }
    header = (
        "# AUTO-GENERATED by scripts/generate_linkml.py - DO NOT EDIT.\n"
        "#\n"
        "# Enums referenced by the resource schemas, folded from two sources:\n"
        "#\n"
        "#   config/enum_manifest.yaml    hand-written manual bindings\n"
        "#   build/fhir_enums.yaml        value sets expanded from the packages\n"
        "#\n"
        "# A manual binding overrides the FHIR default for the same field\n"
        "# (CONVENTIONS.md §12). Only enums a config references are emitted.\n"
        "#\n"
        "# Regenerate with:  uv run scripts/generate_linkml.py\n"
    )
    return (
        header + yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True),
        len(enums),
    )


def _permissible(values: list[Concept]) -> dict[str, Any]:
    """LinkML permissible_values, keeping FHIR codes verbatim."""
    out: dict[str, Any] = {}
    for c in values:
        out[c.code] = {"description": c.display} if c.display else None
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def configured_resources(only: str | None) -> list[tuple[str, Path, dict]]:
    out = []
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        cfg = load_yaml(path)
        name = cfg.get("resource") or path.stem
        if only and name != only:
            continue
        out.append((name, path, cfg))
    if only and not out:
        print(f"ERROR: no config for {only}", file=sys.stderr)
        sys.exit(1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--resource", help="generate only this resource")
    ap.add_argument("--out", type=Path, default=OUTPUT_DIR, help="output directory")
    ap.add_argument(
        "--dry-run", action="store_true", help="print to stdout, write nothing"
    )
    ap.add_argument(
        "--keep-going",
        action="store_true",
        help="report resolution errors as warnings instead of failing",
    )
    args = ap.parse_args()

    global_cfg = load_yaml(GLOBAL_CONFIG)
    rules = GlobalRules.load(global_cfg)
    manifest = (load_yaml(MANIFEST_PATH).get("enums") or {})

    pkgs = global_cfg.get("packages") or {}
    packages = tuple(p for p in (pkgs.get("profile"), pkgs.get("core")) if p)
    if not packages:
        print("ERROR: global.yaml declares no packages", file=sys.stderr)
        sys.exit(1)

    # The two stage-1 caches. Both are pure FHIR and are read, never written,
    # here (CONVENTIONS.md "How a resource becomes a schema").
    for path in (BINDINGS_PATH, FHIR_ENUMS_PATH):
        if not path.exists():
            print(
                f"ERROR: {path.relative_to(REPO_ROOT)} not found. "
                f"Run 'uv run scripts/expand_fhir.py' first.",
                file=sys.stderr,
            )
            sys.exit(1)

    fhir_bindings: dict[str, dict[str, dict]] = {}
    for rec in load_yaml(BINDINGS_PATH).get("bindings") or []:
        fhir_bindings.setdefault(rec["resource"], {})[rec["path"]] = rec
    fhir_enums = load_yaml(FHIR_ENUMS_PATH).get("value_sets") or {}

    expander = ValueSetExpander(packages)
    resolver_bindings = BindingResolver(
        manifest, expander, fhir_bindings, fhir_enums
    )
    exit_code = 0
    written: list[str] = []

    # Every configured resource, regardless of --resource: a Reference target
    # is modelled if a config exists for it, which does not depend on which
    # resource is being generated in this run.
    modelled = frozenset(n for n, _, _ in configured_resources(None))

    for name, cfg_path, cfg in configured_resources(args.resource):
        expanded_path = EXPANDED_DIR / f"{name}.yaml"
        if not expanded_path.exists():
            print(
                f"ERROR [{name}]: {expanded_path.relative_to(REPO_ROOT)} not found. "
                f"Run 'uv run scripts/expand_fhir.py' first.",
                file=sys.stderr,
            )
            exit_code = 1
            continue

        doc = load_yaml(expanded_path)
        elements = prune(doc.get("elements") or [], name, rules)
        tree = ElementTree(name, elements)
        resolver = Resolver(
            tree,
            cfg,
            resolver_bindings,
            strict=not args.keep_going,
            modelled=modelled,
        )

        try:
            schema = resolver.resolve(doc)
        except ResolutionError as exc:
            print(f"ERROR [{name}]: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        rendered = render_schema(schema, cfg_path)

        if args.dry_run:
            print(f"# ===== {name} =====")
            print(rendered)
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            out_path = args.out / f"{name}.yaml"
            out_path.write_text(rendered, encoding="utf-8", newline="\n")
            written.append(
                f"{name:<14} {len(schema.slots):>3} slots  "
                f"{len(schema.classes):>3} classes  -> {out_path.relative_to(REPO_ROOT)}"
            )

        for w in resolver.warnings:
            print(f"  WARNING [{name}]: {w}", file=sys.stderr)

    for w in resolver_bindings.warnings:
        print(f"  WARNING [bindings]: {w}", file=sys.stderr)

    # A manifest entry no config names does not reach cdm/enums.yaml. Say so
    # rather than dropping it silently.
    unused = sorted(set(manifest) - resolver_bindings.used_manual)
    for name in unused:
        print(
            f"  WARNING [enums]: manifest entry `{name}` is referenced by no "
            f"config; not emitted",
            file=sys.stderr,
        )

    # A whitelisted field that FHIR binds, which no config declared. Emitted
    # as a plain field; reported so the omission is visible, never acted on
    # (CONVENTIONS.md "Post-generation checks").
    if resolver_bindings.undeclared:
        print(
            f"\n{len(resolver_bindings.undeclared)} whitelisted fields carry a "
            f"FHIR binding no config declares (emitted as plain fields):"
        )
        for res, path, vs in resolver_bindings.undeclared:
            print(f"  {res:<13} {path:<58} {vs}")

    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        enums_doc, enum_count = render_enums(
            manifest,
            resolver_bindings.used_manual,
            resolver_bindings.derived,
            expander,
        )
        ENUMS_PATH.write_text(enums_doc, encoding="utf-8", newline="\n")

        print()
        for line in written:
            print(line)
        print(
            f"{'enums':<14} {enum_count:>3} enums     "
            f"-> {ENUMS_PATH.relative_to(REPO_ROOT)}"
        )
        if resolver_bindings.redundant:
            print(
                f"\n{len(resolver_bindings.redundant)} manifest entries "
                f"reproduce a value set `default` would have resolved, and can "
                f"be culled (§12):"
            )
            for name, vs in sorted(resolver_bindings.redundant.items()):
                print(f"  {name:<34} {vs}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
