"""Credentials kept on this machine, in a file only this user can read.

What this is, stated plainly so nobody mistakes it for more: a JSON file in the
data directory with mode 0600, and the honest description of that is *not
encrypted at rest*. It protects a credential from other users of the machine
and from anything that copies the project directory; it does not protect it from
this user, from a process running as them, or from a stolen disk.

That is the right size for the first version and it is deliberately behind a
contract. `CredentialStore` is what the platform depends on, so the encrypted
store or the OS keychain that eventually replaces this is a new class in this
package and nothing else - no caller changes, because no caller was ever
allowed to know where a credential lives.

Reading goes through `SecretResolver` as everywhere else, and the environment
wins over the file: a machine that already exports a token should not be
overridden by something a UI wrote months ago, and a user debugging a credential
needs one place they can be sure of.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from domain.errors import SecretNotFoundError, StorageError
from domain.secrets.models import Secret
from domain.secrets.protocols import SecretResolver

log = structlog.get_logger(__name__)

#: Owner read/write, nothing for anybody else.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700


class LocalCredentialStore:
    """Implements `domain.secrets.protocols.CredentialStore` and `SecretResolver`.

    One object for both contracts, like the memory adapter: they are separate so
    that holding the reader does not hand a caller the writer, and the same
    class because reading and writing happen over the same file.
    """

    def __init__(self, path: Path, *, fallback: SecretResolver | None = None) -> None:
        self.path = path
        self._path = path
        self._fallback = fallback

    # --- Reading --------------------------------------------------------------

    def maybe(self, name: str) -> Secret | None:
        if self._fallback is not None and (found := self._fallback.maybe(name)):
            # The environment wins: an exported credential is the one the user
            # can see, and being silently overridden by a stored one is the
            # confusion this ordering avoids.
            return found
        value = self._read().get(name)
        return Secret(name=name, _value=value) if value else None

    def get(self, name: str) -> Secret:
        secret = self.maybe(name)
        if secret is None:
            raise SecretNotFoundError(
                f"No credential named '{name}'. Add it to the integration that "
                "needs it, or export it in the environment."
            )
        return secret

    # --- Writing --------------------------------------------------------------

    async def store(self, name: str, value: str) -> None:
        credentials = self._read()
        credentials[name] = value
        self._write(credentials)
        # The name, never the value, and never its length either.
        log.info("credential.stored", name=name)

    async def forget(self, name: str) -> bool:
        credentials = self._read()
        if credentials.pop(name, None) is None:
            return False
        self._write(credentials)
        log.info("credential.forgotten", name=name)
        return True

    async def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._read()))

    # --- The file -------------------------------------------------------------

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            # Unreadable is not empty. Treating it as empty would silently ask
            # the user to reconnect every service they have, and a UI that
            # cheerfully offers to overwrite an unreadable secrets file is how
            # a recoverable problem becomes a lost credential.
            raise StorageError(f"the credential file at {self._path} cannot be read") from error
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _write(self, credentials: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
        # Written to a neighbour and moved into place, so an interrupted write
        # cannot leave a truncated file where the credentials were. The mode is
        # set before the content is there to be read.
        temporary = self._path.with_suffix(".tmp")
        temporary.touch(mode=FILE_MODE)
        temporary.chmod(FILE_MODE)
        temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
        temporary.replace(self._path)
        self._path.chmod(FILE_MODE)
