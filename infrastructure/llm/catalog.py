"""The model catalog: what is available, what it can do, what it costs.

This is the file you edit to change models. Nothing in `domain/`,
`application/` or an employee declaration names a model, so switching one is a
configuration change and never a code change.

`ModelEntry` itself is a domain value (`domain.llm.catalog`) and is re-exported
here, because this is where every reader of a catalog already looks. What stays
in this module is the reading of a file, which is what infrastructure is for.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.errors import ConfigurationError
from domain.llm.catalog import ModelEntry
from domain.llm.models import TaskKind

DEFAULT_CATALOG_PATH = Path(__file__).parent / "models.toml"


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    entries: tuple[ModelEntry, ...]
    #: Which catalog entry each kind of work prefers, by entry name.
    defaults: dict[TaskKind, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> ModelCatalog:
        source = path or DEFAULT_CATALOG_PATH
        if not source.exists():
            raise ConfigurationError(f"Model catalog not found: {source}")
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
        return cls.from_dict(raw, source=source)

    @classmethod
    def from_dict(cls, raw: dict, *, source: Path | str = "<memory>") -> ModelCatalog:
        entries: list[ModelEntry] = []
        for name, spec in (raw.get("models") or {}).items():
            try:
                entries.append(
                    ModelEntry(
                        name=name,
                        provider=spec["provider"],
                        model=spec["model"],
                        connection=str(spec.get("connection", "")),
                        capabilities=frozenset(
                            Capability(c) for c in spec.get("capabilities", [])
                        ),
                        context_tokens=int(spec.get("context_tokens", 8_192)),
                        input_cost_per_1k_usd=float(spec.get("input_cost_per_1k_usd", 0.0)),
                        output_cost_per_1k_usd=float(spec.get("output_cost_per_1k_usd", 0.0)),
                        quality=float(spec.get("quality", 0.5)),
                        dimensions=int(spec.get("dimensions", 0)),
                    )
                )
            except (KeyError, ValueError) as error:
                raise ConfigurationError(
                    f"Invalid model entry '{name}' in {source}: {error}"
                ) from error

        if not entries:
            raise ConfigurationError(f"Model catalog {source} declares no models")

        known = {entry.name for entry in entries}
        defaults: dict[TaskKind, str] = {}
        for kind, entry_name in (raw.get("defaults") or {}).items():
            try:
                task_kind = TaskKind(kind.upper())
            except ValueError as error:
                raise ConfigurationError(f"Unknown task kind '{kind}' in {source}") from error
            if entry_name not in known:
                raise ConfigurationError(
                    f"Default for '{kind}' points at unknown model '{entry_name}' in {source}"
                )
            defaults[task_kind] = entry_name

        return cls(entries=tuple(entries), defaults=defaults)

    def get(self, name: str) -> ModelEntry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise ConfigurationError(f"Unknown model entry: {name}")

    def find(self, provider: str, model: str) -> ModelEntry | None:
        """Look up an entry by what actually went over the wire, for pricing.

        A provider may answer with a more specific name than it was asked for -
        a dated snapshot of the model requested. That is useful in a trace and
        must not cost the price: an exact match wins, and failing that the
        longest configured name the answer starts with, so `claude-haiku-4-5`
        prices `claude-haiku-4-5-20251001` while never matching a different
        model that merely shares a prefix with a shorter name.
        """
        mine = [entry for entry in self.entries if entry.provider == provider]
        for entry in mine:
            if entry.model == model:
                return entry
        snapshots = [entry for entry in mine if model.startswith(f"{entry.model}-")]
        return max(snapshots, key=lambda entry: len(entry.model), default=None)

    def candidates(self, requirement: CapabilityRequirement) -> list[ModelEntry]:
        return [
            entry
            for entry in self.entries
            if requirement.is_satisfied_by(entry.capabilities)
            and (entry.generates_text or Capability.EMBEDDING in requirement.required)
            and (
                requirement.min_context_tokens is None
                or entry.context_tokens >= requirement.min_context_tokens
            )
            and entry.quality >= requirement.min_quality
        ]
