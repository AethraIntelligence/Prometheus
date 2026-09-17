"""Credentials in the store, sealed, with a cache that keeps the read side sync.

Two contracts, one class, exactly as `LocalCredentialStore` does it: they are
separate so that holding the reader does not hand a caller the writer, and one
class because both work over the same rows.

**Why there is a cache at all.** `SecretResolver.get` is synchronous, and it is
synchronous for a reason worth keeping: it is called from inside a tool at the
moment of the call, and making it a coroutine would put an `await` on the one
path that must not acquire anything. The store is asynchronous. So the sealed
rows are loaded once by `restore` - the same word integrations use for the same
reason - and held as *ciphertext*: what sits in memory is no more readable than
what sits in the database, and the master key opens one value at a time, at the
moment it is asked for.

The cost is honest and small: a credential written by another process is not
seen until this one restores. That is already true of integrations, and the
alternative is an async read on the hot path of every tool call.

Reading order is unchanged from the file store: the environment wins. A machine
that exports a key should not be overridden by something a settings window wrote
months ago, and a person debugging a credential needs one place they can be sure
of.
"""

from __future__ import annotations

import structlog

from domain.errors import SecretNotFoundError
from domain.secrets.models import Secret
from domain.secrets.protocols import SecretResolver
from infrastructure.persistence.secret_repository import SealedSecret, SqlSecretRepository
from infrastructure.secrets.encryption import Envelope

log = structlog.get_logger(__name__)


class EncryptedCredentialStore:
    """Implements `domain.secrets.protocols.CredentialStore` and `SecretResolver`."""

    def __init__(
        self,
        repository: SqlSecretRepository,
        envelope: Envelope,
        *,
        fallback: SecretResolver | None = None,
    ) -> None:
        self._repository = repository
        self._envelope = envelope
        self._fallback = fallback
        self._sealed: dict[str, SealedSecret] = {}

    # --- Reading --------------------------------------------------------------

    def maybe(self, name: str) -> Secret | None:
        if self._fallback is not None and (found := self._fallback.maybe(name)):
            return found
        sealed = self._sealed.get(name)
        if sealed is None:
            return None
        return Secret(name=name, _value=self._envelope.open(name, sealed.ciphertext, sealed.nonce))

    def get(self, name: str) -> Secret:
        secret = self.maybe(name)
        if secret is None:
            raise SecretNotFoundError(
                f"No credential named '{name}'. Add it where it is needed, or "
                "export it in the environment."
            )
        return secret

    # --- Writing --------------------------------------------------------------

    async def store(self, name: str, value: str) -> None:
        ciphertext, nonce = self._envelope.seal(name, value)
        sealed = SealedSecret(name=name, ciphertext=ciphertext, nonce=nonce)
        await self._repository.save(sealed)
        self._sealed[name] = sealed
        # The name, never the value, and never its length either.
        log.info("credential.stored", name=name)

    async def forget(self, name: str) -> bool:
        removed = await self._repository.delete(name)
        self._sealed.pop(name, None)
        if removed:
            log.info("credential.forgotten", name=name)
        return removed

    async def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._sealed))

    # --- Starting -------------------------------------------------------------

    async def restore(self) -> None:
        """Load what is stored. How a fresh process gets its credentials back."""
        self._sealed = {sealed.name: sealed for sealed in await self._repository.list_all()}
        log.info("credentials.restored", count=len(self._sealed))

    async def retire(self, legacy, names: tuple[str, ...]) -> bool:
        """Remove the plaintext file once every value in it is safely here.

        Compared value by value against what this store decrypts, never against
        what it believes it wrote. One mismatch - a name the person changed in
        the window since, or a value that did not arrive - keeps the file, and
        says which name, because that is a decision for a person.
        """
        for name in names:
            found = legacy.maybe(name)
            if found is None:
                continue
            sealed = self._sealed.get(name)
            if sealed is None:
                log.warning("credentials.legacy_kept", name=name, reason="not imported")
                return False
            if self._envelope.open(name, sealed.ciphertext, sealed.nonce) != found.reveal():
                log.warning("credentials.legacy_kept", name=name, reason="values differ")
                return False
        from infrastructure.secrets.encryption import shred

        shred(legacy.path)
        log.info("credentials.legacy_file_removed", count=len(names))
        return True

    async def import_from(self, resolver: SecretResolver, names: tuple[str, ...]) -> int:
        """Take credentials from an older store, without deleting anything there.

        The 0600 JSON file is what installations before Phase 17 have, and the
        upgrade must not be a chore the user discovers by everything failing.
        Nothing is removed from the source: a migration that erases the only
        copy of a credential the moment it thinks it has written another one is
        how a recoverable problem becomes a lost key.
        """
        imported = 0
        for name in names:
            if name in self._sealed:
                continue
            found = resolver.maybe(name)
            if found is None:
                continue
            await self.store(name, found.reveal())
            imported += 1
        if imported:
            log.info("credentials.imported", count=imported)
        return imported
