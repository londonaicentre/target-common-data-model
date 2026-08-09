"""Offline expansion of FHIR value sets from the installed npm packages.

Shared by both stages. Stage 1 expands every packaged value set into
build/fhir_enums.yaml; stage 2 reads that cache and, for a `codesystem`-sourced
manifest entry, resolves a CodeSystem through the same package index.

A binding names a value set; turning it into an enum means listing its concepts
from the packages alone, with no terminology server. Three cases arise, and only
the first two can be answered offline:

  1. The ValueSet ships a pre-computed `expansion.contains` - every UK Core set
     referenced by the CDM does. Authoritative; used as-is.
  2. `compose.include` names a whole CodeSystem with no filter, and that
     CodeSystem is `content: complete` - the HL7 core sets. The concept list is
     the expansion.
  3. `compose.include` carries a filter (a SNOMED `is-a` or `=`). Expanding it
     needs a terminology server, so the binding cannot be resolved here.

There is no concept-count ceiling. Size is not what makes a value set
unusable - expandability is (CONVENTIONS.md "How bindings work").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class Concept:
    code: str
    display: str = ""
    system: str = ""


class ValueSetExpander:
    """Expands a value set to its concepts, using packaged definitions only."""

    def __init__(self, packages: tuple[str, ...]):
        self.packages = packages

    @lru_cache(maxsize=None)
    def _index(self, package: str) -> dict[tuple[str, str], str]:
        """(resourceType, canonical url) -> absolute path, for one package."""
        index_path = REPO_ROOT / "node_modules" / package / ".index.json"
        if not index_path.exists():
            return {}
        pkg_dir = index_path.parent
        out: dict[tuple[str, str], str] = {}
        for entry in json.loads(index_path.read_text(encoding="utf-8")).get("files", []):
            url, rtype = entry.get("url"), entry.get("resourceType")
            if url and rtype:
                out.setdefault((rtype, url), str(pkg_dir / entry["filename"]))
        return out

    def load(self, resource_type: str, url: str) -> dict | None:
        base = url.split("|", 1)[0]
        for package in self.packages:
            path = self._index(package).get((resource_type, base))
            if path:
                return json.loads(Path(path).read_text(encoding="utf-8"))
        return None

    def all_value_set_urls(self) -> list[str]:
        """Canonical URLs of every ValueSet in the installed packages."""
        urls: set[str] = set()
        for package in self.packages:
            urls.update(
                url for (rtype, url) in self._index(package) if rtype == "ValueSet"
            )
        return sorted(urls)

    def expand_all(self) -> dict[str, dict[str, Any]]:
        """Expand every packaged value set, for build/fhir_enums.yaml.

        A terminology cache, not a model: it records what each value set
        resolves to, including the ones that cannot be resolved offline and
        why. Which of these become enums is decided by the resource configs.
        """
        out: dict[str, dict[str, Any]] = {}
        for url in self.all_value_set_urls():
            concepts, note = self.expand(url)
            entry: dict[str, Any] = {"note": note}
            if concepts is None:
                entry["expandable"] = False
            else:
                entry["expandable"] = True
                entry["concept_count"] = len(concepts)
                entry["concepts"] = [
                    {"code": c.code, "display": c.display} for c in concepts
                ]
                if concepts and concepts[0].system:
                    entry["system"] = concepts[0].system
            out[url] = entry
        return out

    def expand(self, url: str) -> tuple[list[Concept] | None, str]:
        """Concepts for `url`, or (None, reason) where it cannot be expanded."""
        vs = self.load("ValueSet", url)
        if vs is None:
            return None, "value set not found in the installed packages"

        # Case 1 - a pre-computed expansion is authoritative.
        contains = (vs.get("expansion") or {}).get("contains")
        if contains:
            concepts = self._flatten_expansion(contains)
            # A UK-wide value set often unions the England and Wales codesets,
            # whose codes collide on different meanings. Collapsing them by
            # code would invent a vocabulary that is neither. §12 covers this:
            # the CDM declares the England codeset as a manual binding instead.
            systems = {c.system for c in concepts if c.system}
            if len(systems) > 1:
                names = ", ".join(sorted(s.rsplit("/", 1)[-1] for s in systems))
                return None, (
                    f"expansion spans {len(systems)} code systems ({names}); "
                    f"declare a manual binding to choose one"
                )
            deduped = self._dedupe(concepts)
            return deduped, f"expansion of {len(deduped)} concepts"

        includes = (vs.get("compose") or {}).get("include") or []
        if not includes:
            return None, "value set has neither an expansion nor a compose"

        out: list[Concept] = []
        for inc in includes:
            if inc.get("filter"):
                ops = ", ".join(f.get("op", "?") for f in inc["filter"])
                return None, f"compose uses a `{ops}` filter; needs a terminology server"
            if inc.get("valueSet"):
                return None, "compose imports another value set"

            system = inc.get("system")
            if not system:
                return None, "compose entry names no system"

            # An inline concept list narrows the system to named codes.
            if inc.get("concept"):
                out.extend(
                    Concept(c["code"], c.get("display", ""), system)
                    for c in inc["concept"]
                )
                continue

            # Case 2 - the whole CodeSystem, which must be complete to be safe.
            cs = self.load("CodeSystem", system)
            if cs is None:
                return None, f"CodeSystem not in packages: {system}"
            if cs.get("content") != "complete":
                return None, (
                    f"CodeSystem {system} is `{cs.get('content')}`, not complete"
                )
            out.extend(
                Concept(c.code, c.display, system)
                for c in self.flatten_codesystem(cs.get("concept") or [])
            )

        if not out:
            return None, "compose resolved to no concepts"

        # As for a pre-computed expansion, a set spanning several code systems
        # is not one vocabulary and is not collapsed into one enum.
        systems = {c.system for c in out if c.system}
        if len(systems) > 1:
            names = ", ".join(sorted(s.rsplit("/", 1)[-1] for s in systems))
            return None, (
                f"compose spans {len(systems)} code systems ({names}); "
                f"declare a manual binding to choose one"
            )
        deduped = self._dedupe(out)
        return deduped, f"compose of {len(deduped)} concepts"

    @staticmethod
    def _dedupe(concepts: list[Concept]) -> list[Concept]:
        """Drop repeated codes, keeping first-seen order."""
        seen: set[str] = set()
        out: list[Concept] = []
        for c in concepts:
            if c.code not in seen:
                seen.add(c.code)
                out.append(c)
        return out

    @staticmethod
    def _flatten_expansion(contains: list[dict]) -> list[Concept]:
        out: list[Concept] = []

        def walk(nodes: list[dict]) -> None:
            for n in nodes:
                if n.get("code"):
                    out.append(
                        Concept(n["code"], n.get("display", ""), n.get("system", ""))
                    )
                walk(n.get("contains") or [])

        walk(contains)
        return out

    @staticmethod
    def flatten_codesystem(concepts: list[dict]) -> list[Concept]:
        """A CodeSystem nests concepts to express hierarchy; the CDM wants
        every code, not the tree."""
        out: list[Concept] = []

        def walk(nodes: list[dict]) -> None:
            for n in nodes:
                if n.get("code"):
                    out.append(Concept(n["code"], n.get("display", "")))
                walk(n.get("concept") or [])

        walk(concepts)
        return out
