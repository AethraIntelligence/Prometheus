"""Read `guide.toml` into `domain.providers.guide` values.

Strict on purpose, like every other declaration here: a recommended model that
names a capability the platform does not have, or a kind no adapter exists for,
is a button that adds an entry the router will never pick - the failure the
guide exists to prevent. So the file is checked when it is loaded, and a test
loads it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from domain.capabilities.models import Capability
from domain.errors import ConfigurationError
from domain.llm.models import TaskKind
from domain.providers.guide import (
    ProviderAdvice,
    ProviderGuide,
    RecommendedModel,
    Requirement,
    Setup,
    SetupStep,
)
from infrastructure.llm.providers import KINDS

GUIDE_PATH = Path(__file__).with_name("guide.toml")

VERDICTS = frozenset({"RECOMMENDED", "SUPPORTED", "NOT_YET"})
ACTIONS = frozenset({"", "link", "connect", "apply"})


def load_guide(path: Path = GUIDE_PATH) -> ProviderGuide:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    kinds = {kind.name for kind in KINDS}
    setups = tuple(_setup(item, kinds) for item in raw.get("setups", ()))
    if len({setup.id for setup in setups}) != len(setups):
        raise ConfigurationError(f"{path.name}: two setups share an id.")
    providers = tuple(
        _provider(item, kinds) for item in raw.get("providers", ())
    )
    return ProviderGuide(
        intro=_text(raw.get("intro", "")),
        setups=setups,
        providers=providers,
        requirements=tuple(
            Requirement(title=item["title"], text=_text(item["text"]))
            for item in raw.get("requirements", ())
        ),
        checked=str(raw.get("checked", "")),
    )


def _setup(item: dict[str, Any], kinds: set[str]) -> Setup:
    if item["kind"] not in kinds:
        raise ConfigurationError(f"Setup '{item['id']}' names unknown kind '{item['kind']}'.")
    steps = tuple(
        SetupStep(
            text=step["text"],
            action=step.get("action", ""),
            url=step.get("url", ""),
            command=step.get("command", ""),
        )
        for step in item.get("steps", ())
    )
    for step in steps:
        if step.action not in ACTIONS:
            raise ConfigurationError(f"Setup '{item['id']}': unknown action '{step.action}'.")
        if step.url and not step.url.startswith("https://"):
            raise ConfigurationError(f"Setup '{item['id']}': links must be https.")
    return Setup(
        id=item["id"],
        title=item["title"],
        badge=item["badge"],
        summary=_text(item["summary"]),
        kind=item["kind"],
        connection_name=item["connection_name"],
        good_for=_text(item.get("good_for", "")),
        steps=steps,
        models=tuple(_model(model, item["id"]) for model in item.get("models", ())),
        cautions=tuple(_text(text) for text in item.get("cautions", ())),
    )


def _model(item: dict[str, Any], setup: str) -> RecommendedModel:
    for capability in item["capabilities"]:
        if capability not in Capability.__members__:
            raise ConfigurationError(f"Setup '{setup}': unknown capability '{capability}'.")
    for kind in item.get("route", ()):
        if kind not in TaskKind.__members__:
            raise ConfigurationError(f"Setup '{setup}': unknown kind of work '{kind}'.")
    return RecommendedModel(
        name=item["name"],
        role=item["role"],
        model=item["model"],
        why=_text(item["why"]),
        capabilities=tuple(item["capabilities"]),
        context_tokens=int(item["context_tokens"]),
        quality=float(item["quality"]),
        input_cost_per_1k_usd=float(item.get("input_cost_per_1k_usd", 0.0)),
        output_cost_per_1k_usd=float(item.get("output_cost_per_1k_usd", 0.0)),
        dimensions=int(item.get("dimensions", 0)),
        route=tuple(item.get("route", ())),
    )


def _provider(item: dict[str, Any], kinds: set[str]) -> ProviderAdvice:
    if item["kind"] not in kinds:
        raise ConfigurationError(f"Provider advice names unknown kind '{item['kind']}'.")
    if item["verdict"] not in VERDICTS:
        raise ConfigurationError(f"Provider '{item['kind']}': unknown verdict.")
    return ProviderAdvice(
        kind=item["kind"], label=item["label"], verdict=item["verdict"], text=_text(item["text"])
    )


def _text(value: str) -> str:
    """TOML's multi-line strings keep their line breaks; a screen wraps on its own."""
    return " ".join(value.split())
