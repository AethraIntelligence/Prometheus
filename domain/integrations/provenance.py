"""What a plugin runs, pinned, and the evidence that it is what was reviewed.

A plugin declaration names a command - `npx`, `uvx`, `docker` - and the package
that command fetches is the code that actually runs with the person's tokens.
Before Phase 13 every shipped declaration named a package without a version, so
what ran was whatever its registry served that day. Two things close that.

**The declaration is locked.** `plugins/catalog.lock.json` holds a digest of each
plugin's files. A declaration whose files do not match - edited, replaced, or
added without being locked - is not offered and cannot be installed. Locking is
a release step (`scripts/release/lock_plugins.py`), reviewed like code.

**The artifact is pinned and its published digest is locked.** The declaration's
arguments name an exact version (`name@1.2.3`) or an image digest
(`image@sha256:...`), and the lock records the digest the registry published for
exactly that version. Before a plugin is installed, the registry is asked again
and must give the same answer; the package manager then checks the download
against that same published digest. A registry that now serves different bytes
under the same version is refused before anything starts - and so is a registry
that cannot be reached, because an install that could not be verified is not
verified.

What this does not reach, said plainly: the packages *those* packages depend on
are resolved by their own package manager at the time of the run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from domain.errors import PluginVerificationError

__all__ = [
    "ArtifactKind",
    "ArtifactVerifier",
    "PluginArtifact",
    "PluginVerificationError",
    "declaration_digest",
    "pinned_in",
    "same_published",
]


class ArtifactKind(StrEnum):
    NPM = "npm"
    PYPI = "pypi"
    OCI = "oci"


@dataclass(frozen=True, slots=True)
class PluginArtifact:
    kind: ArtifactKind
    name: str
    version: str
    #: npm: one `sha512-...` integrity. PyPI: the sha256 of every published file
    #: of the version. OCI: the manifest digest, which is also the version.
    digests: tuple[str, ...]

    @property
    def reference(self) -> str:
        """How the declaration's arguments must name it: `name@1.2.3`, `image@sha256:...`."""
        return f"{self.name}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "digests": list(self.digests),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PluginArtifact:
        digests = raw.get("digests")
        if not isinstance(digests, list) or not digests:
            raise ValueError("an artifact needs at least one digest")
        return cls(
            kind=ArtifactKind(raw["kind"]),
            name=str(raw["name"]),
            version=str(raw["version"]),
            digests=tuple(str(item) for item in digests),
        )


def declaration_digest(files: Iterable[tuple[str, bytes]]) -> str:
    """One digest over a plugin's files, independent of the order they were read in."""
    digest = hashlib.sha256()
    for name, content in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def pinned_in(args: Iterable[str], artifact: PluginArtifact) -> bool:
    """Whether the arguments run exactly the locked artifact and nothing looser."""
    return artifact.reference in tuple(args)


def same_published(locked: PluginArtifact, published: tuple[str, ...]) -> bool:
    """The registry still publishes what was locked. PyPI may add files, never change them."""
    if locked.kind is ArtifactKind.PYPI:
        return bool(published) and set(locked.digests) <= set(published)
    return set(locked.digests) == set(published)


class ArtifactVerifier(Protocol):
    """Asks the artifact's registry what it publishes for the pinned version."""

    async def published(self, artifact: PluginArtifact) -> tuple[str, ...]: ...
