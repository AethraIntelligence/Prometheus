"""Advice about which provider and which models to start with, as values.

The advice itself names products, so it lives in a file beside the adapters
(`infrastructure/llm/guide.toml`) and nowhere under `domain/`. What is here is
its shape - which is what lets the window render it without knowing a single
vendor, and lets "add these models" be one operation in the core rather than a
list of calls the window strings together.

**A setup is applied, not only read.** A person who has just been told which
models to use should not then have to type the model id, tick six capability
boxes correctly and route seven kinds of work by hand; every one of those is a
place to get it quietly wrong. So each recommended model carries exactly what an
entry needs, and `route` says which kinds of work it should take.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecommendedModel:
    #: The catalog entry name it is added under.
    name: str
    #: What it is for, in a person's words: "Planning, acting and answering".
    role: str
    #: The id the provider knows it by.
    model: str
    why: str
    capabilities: tuple[str, ...]
    context_tokens: int
    quality: float
    input_cost_per_1k_usd: float = 0.0
    output_cost_per_1k_usd: float = 0.0
    dimensions: int = 0
    #: Empty follows the provider default. LOCAL/REMOTE handles local servers
    #: that forward selected models to a vendor cloud.
    privacy: str = ""
    latency_ms: int = 0
    #: Kinds of work (TaskKind values) to send to it when the setup is applied.
    route: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SetupStep:
    text: str
    #: "link" opens `url`; "connect" adds a connection of the setup's kind;
    #: "apply" adds the models and routes work. Empty is a step to read.
    action: str = ""
    url: str = ""
    #: A command to copy, for a step done in a terminal.
    command: str = ""


@dataclass(frozen=True, slots=True)
class Setup:
    id: str
    title: str
    #: One or two words on the card: "Free", "Best results", "Private".
    badge: str
    summary: str
    #: The provider kind every model in it is reached through.
    kind: str
    #: A name to suggest for the connection it needs.
    connection_name: str
    good_for: str
    steps: tuple[SetupStep, ...]
    models: tuple[RecommendedModel, ...]
    cautions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderAdvice:
    kind: str
    label: str
    #: RECOMMENDED, SUPPORTED or NOT_YET - how strongly to reach for it.
    verdict: str
    text: str


@dataclass(frozen=True, slots=True)
class Requirement:
    """One thing a model has to be able to do here, and what happens without it."""

    title: str
    text: str


@dataclass(frozen=True, slots=True)
class ProviderGuide:
    intro: str
    setups: tuple[Setup, ...]
    providers: tuple[ProviderAdvice, ...]
    requirements: tuple[Requirement, ...]
    #: When the advice was last checked against the providers, as YYYY-MM-DD.
    checked: str = ""

    def setup(self, setup_id: str) -> Setup | None:
        return next((item for item in self.setups if item.id == setup_id), None)
