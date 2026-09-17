"""The master key in the operating system's credential vault.

`keyring` is the adapter: the macOS Keychain, the Windows Credential Locker, or
the Secret Service on Linux. What it stores is the base64 of the 256-bit key,
under one service name and an account derived from the data directory, so two
installations on one login - a test profile beside a real one - do not share a
key by accident.

A backend `keyring` reports as unusable - its null or fail backend, or anything
with a priority below one - is *not available*, and the caller refuses to start
rather than writing the key somewhere weaker. That refusal names the setting
that chooses a file on purpose, because a headless machine is a real case and
the answer to it is a decision, not a fallback.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from domain.errors import StorageError

SERVICE = "app.prometheus.desktop"
KEY_BYTES = 32


def account_for(data_dir: Path) -> str:
    digest = hashlib.sha256(str(data_dir.expanduser().resolve()).encode("utf-8")).hexdigest()
    return f"master-key-{digest[:16]}"


class KeychainVault:
    """Implements `domain.secrets.protocols.KeyVault` over `keyring`."""

    name = "keychain"

    def __init__(self, data_dir: Path, *, backend=None) -> None:
        self._account = account_for(data_dir)
        self._backend = backend

    def _keyring(self):
        if self._backend is not None:
            return self._backend
        import keyring

        return keyring.get_keyring()

    def available(self) -> bool:
        try:
            backend = self._keyring()
        except Exception:
            return False
        module = type(backend).__module__
        if module.startswith(("keyring.backends.fail", "keyring.backends.null")):
            return False
        try:
            return float(getattr(backend, "priority", 0)) >= 1
        except Exception:
            return False

    def read(self) -> bytes | None:
        try:
            stored = self._keyring().get_password(SERVICE, self._account)
        except Exception as error:
            raise StorageError(
                f"The system keychain could not be read ({type(error).__name__}). Unlock it "
                "and start Prometheus again."
            ) from error
        if stored is None:
            return None
        try:
            key = base64.urlsafe_b64decode(stored)
        except ValueError as error:
            raise StorageError("The master key in the system keychain is damaged.") from error
        if len(key) != KEY_BYTES:
            raise StorageError("The master key in the system keychain is the wrong length.")
        return key

    def write(self, key: bytes) -> None:
        try:
            self._keyring().set_password(
                SERVICE, self._account, base64.urlsafe_b64encode(key).decode("ascii")
            )
        except Exception as error:
            raise StorageError(
                f"The system keychain refused the master key ({type(error).__name__}). Unlock "
                "it and start Prometheus again."
            ) from error

    def delete(self) -> None:
        try:
            self._keyring().delete_password(SERVICE, self._account)
        except Exception:
            return
