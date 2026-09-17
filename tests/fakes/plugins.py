"""Locking a plugin directory a test declared, and a registry the test controls."""

from __future__ import annotations

import json
from pathlib import Path

from domain.integrations.provenance import PluginArtifact, declaration_digest


def lock(plugins: Path, artifacts: dict[str, PluginArtifact] | None = None) -> Path:
    """Write `catalog.lock.json` describing every plugin directory as it is now."""
    entries = {}
    for directory in sorted(path for path in plugins.iterdir() if path.is_dir()):
        files = [
            (item.name, item.read_bytes())
            for item in sorted(directory.iterdir())
            if item.is_file()
        ]
        artifact = (artifacts or {}).get(directory.name)
        entries[directory.name] = {
            "declaration_sha256": declaration_digest(files),
            "artifact": artifact.to_dict() if artifact else None,
        }
    path = plugins / "catalog.lock.json"
    path.write_text(json.dumps({"format_version": 1, "plugins": entries}), encoding="utf-8")
    return path


class ScriptedRegistry:
    """Implements `domain.integrations.provenance.ArtifactVerifier` from a dictionary."""

    def __init__(self, published: dict[str, tuple[str, ...]] | None = None, *, down: bool = False):
        self._published = published or {}
        self._down = down
        self.asked: list[str] = []

    async def published(self, artifact: PluginArtifact) -> tuple[str, ...]:
        from domain.integrations.provenance import PluginVerificationError

        self.asked.append(artifact.reference)
        if self._down:
            raise PluginVerificationError(
                f"{artifact.reference} could not be verified with its registry. "
                "It was not installed."
            )
        return self._published.get(artifact.reference, ())
