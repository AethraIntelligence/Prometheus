"""What a registry is asked for a pinned plugin artifact, against a scripted registry."""

from __future__ import annotations

import httpx
import pytest

from domain.integrations.provenance import (
    ArtifactKind,
    PluginArtifact,
    PluginVerificationError,
    declaration_digest,
)
from infrastructure.integrations.provenance import RegistryArtifactVerifier


def verifier(handler) -> RegistryArtifactVerifier:
    return RegistryArtifactVerifier(transport=httpx.MockTransport(handler))


async def test_npm_answers_with_the_versions_integrity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tavily-mcp/0.2.22"
        return httpx.Response(200, json={"dist": {"integrity": "sha512-abc"}})

    artifact = PluginArtifact(ArtifactKind.NPM, "tavily-mcp", "0.2.22", ("sha512-abc",))

    assert await verifier(handler).published(artifact) == ("sha512-abc",)


async def test_pypi_answers_with_every_file_of_the_release() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pypi/mcp-server-time/2026.8.18/json"
        files = [{"digests": {"sha256": "b" * 64}}, {"digests": {"sha256": "a" * 64}}]
        return httpx.Response(200, json={"urls": files})

    artifact = PluginArtifact(ArtifactKind.PYPI, "mcp-server-time", "2026.8.18", ("a" * 64,))

    assert await verifier(handler).published(artifact) == ("a" * 64, "b" * 64)


async def test_an_image_answers_with_its_manifest_digest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anonymous"})
        assert request.headers["authorization"] == "Bearer anonymous"
        return httpx.Response(200, headers={"docker-content-digest": "sha256:" + "c" * 64})

    digest = "sha256:" + "c" * 64
    artifact = PluginArtifact(ArtifactKind.OCI, "ghcr.io/org/server", digest, (digest,))

    assert await verifier(handler).published(artifact) == (digest,)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(404),
        lambda request: httpx.Response(200, json={"unexpected": True}),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    ],
    ids=["missing version", "unexpected answer", "offline"],
)
async def test_anything_but_a_clear_answer_refuses(handler) -> None:
    artifact = PluginArtifact(ArtifactKind.NPM, "tool", "1.0.0", ("sha512-x",))

    with pytest.raises(PluginVerificationError, match="not installed"):
        await verifier(handler).published(artifact)


def test_a_declaration_digest_does_not_depend_on_reading_order() -> None:
    files = [("plugin.yaml", b"id: a"), ("icon.svg", b"<svg/>")]

    assert declaration_digest(files) == declaration_digest(list(reversed(files)))
    assert declaration_digest(files) != declaration_digest([("plugin.yaml", b"id: b")])
