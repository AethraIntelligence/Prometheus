"""Which kinds of provider this machine can talk to, and what each one needs.

The list lives here because this is where the adapters are: `factory.py` maps a
kind to a class, and a kind with no class behind it is a record the user can
create and never use. It is deliberately not in `domain/` - no vendor is named
there, and the architecture test fails the build over it.

`needs_credential` is the only thing a caller above the adapters has to know,
and it exists because of the one kind that does not: a model runner on this
machine has no account behind it, and demanding a key for it would make the one
setup that needs no configuration need some.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class ProviderKind:
    """One kind of provider, as a person choosing one sees it."""

    name: str
    label: str
    needs_credential: bool = True
    #: Shown beside the field where a person types an address, and used when
    #: they leave it empty for a kind that has no fixed one.
    default_base_url: str = ""


#: Every kind `ProviderFactory` can build a client for, and nothing else.
KINDS: tuple[ProviderKind, ...] = (
    ProviderKind("openai", "OpenAI"),
    ProviderKind("anthropic", "Anthropic"),
    ProviderKind("gemini", "Google Gemini"),
    ProviderKind("openrouter", "OpenRouter"),
    #: Answers typed decisions, never text: it is routed only the `decision`
    #: kind of work (`infrastructure/decisions/`).
    ProviderKind(
        "typesafe", "TypeSafe (decisions)", default_base_url="https://api.typesafe.ai"
    ),
    ProviderKind(
        "local",
        "Local model runner",
        needs_credential=False,
        default_base_url="http://127.0.0.1:11434/v1",
    ),
)

_BY_NAME = {kind.name: kind for kind in KINDS}


def kind_named(name: str) -> ProviderKind:
    """The kind, or an error naming what this machine actually has.

    Refused rather than accepted-and-ignored: a connection of an unknown kind
    would sit in the settings window looking configured and fail at the moment
    of the first call, which is the furthest possible point from the mistake.
    """
    kind = _BY_NAME.get(name.strip().lower())
    if kind is None:
        known = ", ".join(sorted(_BY_NAME))
        raise ConfigurationError(f"Unknown provider kind '{name}'. This machine has: {known}.")
    return kind
