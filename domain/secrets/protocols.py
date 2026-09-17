from __future__ import annotations

from typing import Protocol

from domain.secrets.models import Secret


class SecretResolver(Protocol):
    """Where a tool gets a credential from, at the moment it needs one.

    Tools are handed the resolver, never the value: a tool built at start-up
    with a key baked into it puts that key in every trace of its construction.
    """

    def get(self, name: str) -> Secret: ...

    def maybe(self, name: str) -> Secret | None: ...


class KeyVault(Protocol):
    """Where the one key that opens every stored credential lives, apart from the store.

    Kept outside the database by construction: a backup, a dump or a copied
    data directory carries ciphertext and never this. The operating system's
    credential vault is the implementation a desktop gets; a headless machine
    says its key in the environment or opts into a file, and never silently
    falls back to one.
    """

    name: str

    def available(self) -> bool: ...

    def read(self) -> bytes | None: ...

    def write(self, key: bytes) -> None: ...

    def delete(self) -> None: ...


class CredentialStore(Protocol):
    """The side that can *write* a credential, kept apart from the side that reads.

    Split for the same reason `MemoryMaintenance` is split from `Memory`: a tool
    holding a `SecretResolver` so it can use a token must not thereby be able to
    replace or delete one. Almost everything in the platform holds the reader;
    the writer is held by the one place a person connects a service.

    `store` takes the value as a plain string because that is what arrives from
    the interface, and it is the last moment it exists as one - what comes back
    out is a `Secret`, which does not print itself.
    """

    async def store(self, name: str, value: str) -> None: ...

    async def forget(self, name: str) -> bool: ...

    async def names(self) -> tuple[str, ...]: ...
