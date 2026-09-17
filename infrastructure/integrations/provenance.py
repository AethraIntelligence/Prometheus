"""What a registry publishes for one pinned artifact, asked over HTTPS.

Implements `domain.integrations.provenance.ArtifactVerifier` against the three
registries the shipped plugins use: npm (the version's `dist.integrity`), PyPI
(the sha256 of every file of the release) and an OCI registry (the manifest
digest of the pinned reference, which must equal the reference itself).

Every failure - a network error, a missing version, an answer in an unexpected
shape - raises `PluginVerificationError`. There is no "could not check, carry
on": an install that could not be verified is refused.
"""

from __future__ import annotations

import httpx

from domain.integrations.provenance import (
    ArtifactKind,
    PluginArtifact,
    PluginVerificationError,
)


class RegistryArtifactVerifier:
    """Implements `domain.integrations.provenance.ArtifactVerifier`."""

    OCI_ACCEPT = ", ".join(
        (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        # Injected by tests; the suite never reaches a registry.
        self._transport = transport

    async def published(self, artifact: PluginArtifact) -> tuple[str, ...]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True, transport=self._transport
            ) as client:
                return await self._ask(client, artifact)
        except PluginVerificationError:
            raise
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
            raise PluginVerificationError(
                f"{artifact.reference} could not be verified with its registry "
                f"({type(error).__name__}). It was not installed."
            ) from error

    def published_sync(self, client: httpx.Client, artifact: PluginArtifact) -> tuple[str, ...]:
        """The same question from a script, which has no event loop to spare."""
        import asyncio

        async def ask() -> tuple[str, ...]:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as inner:
                return await self._ask(inner, artifact)

        del client
        return asyncio.run(ask())

    async def _ask(self, client: httpx.AsyncClient, artifact: PluginArtifact) -> tuple[str, ...]:
        if artifact.kind is ArtifactKind.NPM:
            answer = await client.get(
                f"https://registry.npmjs.org/{artifact.name}/{artifact.version}"
            )
            answer.raise_for_status()
            return (str(answer.json()["dist"]["integrity"]),)
        if artifact.kind is ArtifactKind.PYPI:
            answer = await client.get(
                f"https://pypi.org/pypi/{artifact.name}/{artifact.version}/json"
            )
            answer.raise_for_status()
            files = answer.json()["urls"]
            digests = tuple(sorted(str(item["digests"]["sha256"]) for item in files))
            if not digests:
                raise PluginVerificationError(f"{artifact.reference} publishes no files.")
            return digests
        registry, _, repository = artifact.name.partition("/")
        token = await client.get(
            f"https://{registry}/token",
            params={"scope": f"repository:{repository}:pull", "service": registry},
        )
        token.raise_for_status()
        answer = await client.head(
            f"https://{registry}/v2/{repository}/manifests/{artifact.version}",
            headers={
                "Authorization": f"Bearer {token.json()['token']}",
                "Accept": self.OCI_ACCEPT,
            },
        )
        answer.raise_for_status()
        return (str(answer.headers["docker-content-digest"]),)
