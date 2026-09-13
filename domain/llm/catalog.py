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

from domain.capabilities.models import Capability
from domain.llm.models import ModelChoice

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

    @property
    def embeds(self) -> bool:
        return Capability.EMBEDDING in self.capabilities

    @property
    def generates_text(self) -> bool:
        """False for an entry that only turns text into vectors.

        Such an entry satisfies any requirement that names no capability - and
        the manager's own stages name none - so without this a chat request was
        sent to `nomic-embed-text`, which answered "does not support chat".
        """
        return not self.capabilities or self.capabilities != frozenset({Capability.EMBEDDING})

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
