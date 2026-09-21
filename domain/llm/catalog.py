"""What one model is: what it can do, what it costs, how it is reached.

A value, not a file. The *loading* of a catalog is infrastructure - TOML on
disk, rows in a store - and lives there; what an entry is has to be here,
because the application layer administers these and may not import
infrastructure (ADR 0001).

No vendor is named. `provider` and `model` are strings the adapters interpret,
exactly as `ModelChoice` has always carried them, and `connection` is the name
of a record somebody added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite

from domain.capabilities.models import Capability
from domain.llm.models import ModelChoice


def default_privacy(provider: str) -> Privacy:
    """What an entry that did not say is assumed to be: a model this machine
    serves is local, anything else is not. An entry forwarded elsewhere by a
    local server has to say REMOTE, and the shipped local catalog does."""
    return Privacy.LOCAL if provider.strip().lower() == "local" else Privacy.REMOTE


class Privacy(StrEnum):
    """Where a prompt goes when this model is called.

    LOCAL never leaves the machine. REMOTE is anyone else's computer - a hosted
    provider, or a local server that forwards to its vendor's cloud. Declared
    rather than guessed from the provider name, because the second case is real
    and ships in the local catalog.
    """

    LOCAL = "LOCAL"
    REMOTE = "REMOTE"

#: Capabilities that describe a model which does not complete prompts. An
#: entry offering nothing else is kept out of every piece of text work.
NON_TEXT = frozenset({Capability.EMBEDDING, Capability.DECISION})

#: What an entry is assumed to take when nobody said and nothing could be asked.
DEFAULT_CONTEXT_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class ModelEntry:
    name: str
    provider: str
    model: str
    #: The connection this entry is reached through, by name. Empty means the
    #: key and address the machine itself was configured with, which is every
    #: entry in the shipped file.
    connection: str = ""
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    context_tokens: int = DEFAULT_CONTEXT_TOKENS
    input_cost_per_1k_usd: float = 0.0
    output_cost_per_1k_usd: float = 0.0
    #: Rough, hand-maintained quality ranking used to break ties. It is a
    #: preference order, not a benchmark.
    quality: float = 0.5
    #: How many numbers this model's vectors have. Nothing but an embedding
    #: entry sets it, and it is here rather than discovered because a store
    #: full of vectors of one size has to be able to say so before a query is
    #: made rather than after it returns a wrong answer (ADR 0016).
    dimensions: int = 0
    #: What an embedding model wants put in front of a question and in front of
    #: a passage. Several of them - the e5 family, nomic-embed-text - were
    #: trained with asymmetric prefixes and score a question against a passage
    #: several points lower without them, which is a worse answer nobody can
    #: see. Declared per entry because it belongs to the model, exactly as
    #: `dimensions` does, and empty for a model that wants none (bge-m3).
    query_prefix: str = ""
    passage_prefix: str = ""
    privacy: Privacy = Privacy.REMOTE
    #: Typical time to a full answer, in milliseconds, as measured or stated.
    #: Zero is unknown, which ranks as neither fast nor slow.
    latency_ms: int = 0

    def __post_init__(self) -> None:
        """Reject contracts whose numbers would invert or bypass routing.

        Cost and latency participate directly in ranking, while quality picks
        the escalation tier. Negative or out-of-range values are therefore not
        harmless metadata: they can make an invalid model win every route.
        """
        if self.context_tokens <= 0:
            raise ValueError("context_tokens must be greater than zero")
        if not all(
            isfinite(cost) and cost >= 0
            for cost in (self.input_cost_per_1k_usd, self.output_cost_per_1k_usd)
        ):
            raise ValueError("model costs must be finite and non-negative")
        if not isfinite(self.quality) or not 0 <= self.quality <= 1:
            raise ValueError("quality must be between 0 and 1")
        if self.dimensions < 0:
            raise ValueError("dimensions cannot be negative")
        if self.latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")

    @property
    def embeds(self) -> bool:
        return Capability.EMBEDDING in self.capabilities

    @property
    def decides(self) -> bool:
        """Answers typed questions without writing text (`domain/decisions/`)."""
        return Capability.DECISION in self.capabilities

    @property
    def generates_text(self) -> bool:
        """False for an entry that only turns text into vectors, or only decides.

        Such an entry satisfies any requirement that names no capability - and
        the manager's own stages name none - so without this a chat request was
        sent to `nomic-embed-text`, which answered "does not support chat". A
        model built to decide is the same case: it takes a question and options,
        and a plan prompt sent to it would come back as an error.
        """
        return not self.capabilities or not self.capabilities <= NON_TEXT

    def cost_of(self, prompt_tokens: int, output_tokens: int) -> float:
        return (
            prompt_tokens * self.input_cost_per_1k_usd
            + output_tokens * self.output_cost_per_1k_usd
        ) / 1000

    @property
    def choice(self) -> ModelChoice:
        return ModelChoice(
            provider=self.provider, model=self.model, connection=self.connection
        )

    @property
    def contract(self) -> ModelContract:
        return ModelContract.of(self)


#: Quality bands. A tier is what escalation moves between: two entries a
#: hundredth apart in hand-maintained quality are not a step up.
TIERS: tuple[tuple[float, str], ...] = (
    (0.75, "STRONG"),
    (0.55, "BALANCED"),
    (0.0, "FAST"),
)


def tier_of(quality: float) -> str:
    return next(name for floor, name in TIERS if quality >= floor)


@dataclass(frozen=True, slots=True)
class ModelContract:
    """What routing may rely on a model for, stated in one place.

    Everything a decision reads - capabilities, context, quality tier, privacy,
    latency, cost - and nothing a decision may not. Whether the model actually
    lives up to it is not a declaration at all: it is the validation record
    for the profile it runs in (`domain/validation/gate.py`).
    """

    entry: str
    capabilities: frozenset[Capability]
    context_tokens: int
    quality: float
    tier: str
    privacy: Privacy
    latency_ms: int
    input_cost_per_1k_usd: float
    output_cost_per_1k_usd: float

    @classmethod
    def of(cls, entry: ModelEntry) -> ModelContract:
        return cls(
            entry=entry.name,
            capabilities=entry.capabilities,
            context_tokens=entry.context_tokens,
            quality=entry.quality,
            tier=tier_of(entry.quality),
            privacy=entry.privacy,
            latency_ms=entry.latency_ms,
            input_cost_per_1k_usd=entry.input_cost_per_1k_usd,
            output_cost_per_1k_usd=entry.output_cost_per_1k_usd,
        )

    @property
    def estimated_cost_per_1k_usd(self) -> float:
        """Input plus a quarter of output: precise enough to rank, only used to rank."""
        return self.input_cost_per_1k_usd + self.output_cost_per_1k_usd / 4
