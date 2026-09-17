"""The master key, and the envelope every stored credential sits in.

Phase 17 puts credentials in the store rather than in a file beside it, because
the backend has to be deployable: a container that restarts with an empty
filesystem loses a file and keeps its database. That is only safe if what
reaches the database is unreadable without something the database does not have.

**The master key is that something, and it never goes near the store.**
`PROMETHEUS_MASTER_KEY` first - that is how a server is configured, and it is the
only sensible answer for a process with no persistent disk. Without it, the key
lives in a `KeyVault`: since Phase 13 that is the operating system's credential
vault on a desktop, and a 0600 file only where a headless machine chose one
(`PROMETHEUS_SECRET_BACKEND=file`). There is no quiet fallback from one to the
other - a desktop whose keychain is locked is told so, not handed a file.

**Moving a key from the file into the vault is repeatable and leaves no copy.**
Write to the vault, read it back, compare, and only then overwrite and remove the
file. A crash between any two steps leaves either the file alone, or both copies
equal - and the next start finishes the move. Two copies that *differ* are never
resolved by guessing: every stored credential was sealed with one of them.

AES-GCM, 256-bit, a fresh nonce per write, and the credential's own name as
associated data - so a ciphertext moved to another row stops decrypting rather
than quietly becoming a different credential's value.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from domain.errors import ConfigurationError, StorageError
from domain.secrets.protocols import KeyVault

log = structlog.get_logger(__name__)

#: Where a deployment says what the key is.
MASTER_KEY_ENV = "PROMETHEUS_MASTER_KEY"
#: Owner read/write, nothing for anybody else.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700
KEY_BITS = 256
NONCE_BYTES = 12


def resolve_master_key(
    data_dir: Path,
    environ: dict[str, str] | None = None,
    *,
    vault: KeyVault | None = None,
) -> bytes:
    """The key, from the environment if it is there and from a file if it is not.

    Generating one on first use rather than refusing is deliberate: a desktop
    user who has never heard of a master key still gets encrypted credentials,
    and the alternative - refuse until configured - is the kind of ceremony that
    ends with people storing keys in plain text somewhere else.
    """
    environment = environ if environ is not None else dict(os.environ)
    raw = environment.get(MASTER_KEY_ENV, "").strip()
    if raw:
        try:
            key = base64.urlsafe_b64decode(raw)
        except (ValueError, TypeError) as error:
            raise ConfigurationError(
                f"{MASTER_KEY_ENV} is not valid base64. It must be a base64-encoded "
                f"{KEY_BITS // 8}-byte key."
            ) from error
        if len(key) != KEY_BITS // 8:
            raise ConfigurationError(
                f"{MASTER_KEY_ENV} must decode to {KEY_BITS // 8} bytes, got {len(key)}."
            )
        return key
    if vault is None:
        return _from_file(data_dir / "master.key")
    return _from_vault(vault, data_dir / "master.key")


def store_master_key(data_dir: Path, key: bytes, *, vault: KeyVault | None = None) -> None:
    """Put a key brought from another machine where this one keeps its key.

    Used by a restore that carried sealed secrets. The key replaces whatever key
    this data directory had, because the database it is restored beside was
    sealed with it.
    """
    if len(key) != KEY_BITS // 8:
        raise StorageError("A master key must be 32 bytes.")
    if vault is not None:
        if not vault.available():
            raise ConfigurationError(
                f"The system credential vault ({vault.name}) is not available."
            )
        vault.write(key)
        if vault.read() != key:
            raise StorageError(f"The {vault.name} did not keep the master key it was given.")
        return
    path = data_dir / "master.key"
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    temporary = path.with_suffix(".tmp")
    temporary.touch(mode=FILE_MODE)
    temporary.chmod(FILE_MODE)
    temporary.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
    temporary.replace(path)
    path.chmod(FILE_MODE)


def _from_vault(vault: KeyVault, legacy: Path) -> bytes:
    if not vault.available():
        raise ConfigurationError(
            f"The system credential vault ({vault.name}) is not available, so the key that "
            "encrypts stored credentials has nowhere safe to live. Unlock or install it "
            "(the login keychain, Windows Credential Manager, or a Secret Service such as "
            "GNOME Keyring). On a headless machine, set PROMETHEUS_MASTER_KEY, or choose a "
            "file on purpose with PROMETHEUS_SECRET_BACKEND=file."
        )
    stored = vault.read()
    on_disk = _from_file(legacy) if legacy.exists() else None
    if stored is not None and on_disk is not None and stored != on_disk:
        raise StorageError(
            f"The master key in the {vault.name} differs from the one in {legacy}. Nothing "
            "was changed. Credentials were sealed with one of them; move the wrong one away "
            "and start again."
        )
    if stored is None:
        key = on_disk if on_disk is not None else AESGCM.generate_key(bit_length=KEY_BITS)
        vault.write(key)
        if vault.read() != key:
            raise StorageError(
                f"The {vault.name} did not keep the master key it was given. Nothing was "
                "removed; start again once it is unlocked."
            )
        log.info("secrets.master_key_in_vault", vault=vault.name, moved=on_disk is not None)
        stored = key
    if on_disk is not None:
        shred(legacy)
        log.info("secrets.master_key_file_removed", path=str(legacy))
    return stored


def shred(path: Path) -> None:
    """Overwrite a secret file before removing it.

    Not a guarantee on a journaling or copy-on-write filesystem, and not claimed
    as one; it keeps the plaintext out of the directory and out of a naive
    recovery of the freed blocks, and the rest is what full-disk encryption is for.
    """
    try:
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(os.urandom(max(size, 1)))
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return
    path.unlink(missing_ok=True)


def _from_file(path: Path) -> bytes:
    if path.exists():
        try:
            key = base64.urlsafe_b64decode(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as error:
            # Unreadable is not missing. Generating a new key here would leave
            # every stored credential undecryptable with no way back and no
            # message saying what happened.
            raise StorageError(f"the master key at {path} cannot be read") from error
        if len(key) != KEY_BITS // 8:
            raise StorageError(f"the master key at {path} is the wrong length.")
        return key

    key = AESGCM.generate_key(bit_length=KEY_BITS)
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    temporary = path.with_suffix(".tmp")
    temporary.touch(mode=FILE_MODE)
    temporary.chmod(FILE_MODE)
    temporary.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
    temporary.replace(path)
    path.chmod(FILE_MODE)
    log.info("secrets.master_key_created", path=str(path))
    return key


class Envelope:
    """Encrypts and decrypts one value at a time. Holds the key and nothing else."""

    def __init__(self, key: bytes) -> None:
        self._cipher = AESGCM(key)

    def seal(self, name: str, value: str) -> tuple[bytes, bytes]:
        """The ciphertext and the nonce that produced it."""
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._cipher.encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
        return sealed, nonce

    def open(self, name: str, sealed: bytes, nonce: bytes) -> str:
        try:
            return self._cipher.decrypt(nonce, sealed, name.encode("utf-8")).decode("utf-8")
        except (InvalidTag, ValueError) as error:
            # The wrong key, a tampered row, or a value written under a key that
            # has since been replaced. All three mean the same thing to a
            # caller, and none of them may be answered with a guess.
            raise StorageError(
                f"the credential '{name}' cannot be decrypted with this master key"
            ) from error
