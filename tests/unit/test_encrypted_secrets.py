"""Phase 17: a credential in the store is unreadable without the master key.

The claim this file defends is the one that makes storing keys in the database
better than storing them beside it: what a backup, a dump or `storage-migrate`
carries is ciphertext, and the key that opens it was never in there.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from domain.errors import ConfigurationError, SecretNotFoundError, StorageError
from domain.secrets.models import Secret
from infrastructure.persistence.secret_repository import SealedSecret
from infrastructure.secrets.encrypted import EncryptedCredentialStore
from infrastructure.secrets.encryption import (
    MASTER_KEY_ENV,
    Envelope,
    resolve_master_key,
)


class InMemorySecretRepository:
    """The storage half, without a database. Holds ciphertext, like the real one."""

    def __init__(self) -> None:
        self.rows: dict[str, SealedSecret] = {}

    async def save(self, secret: SealedSecret) -> None:
        self.rows[secret.name] = secret

    async def list_all(self) -> list[SealedSecret]:
        return list(self.rows.values())

    async def delete(self, name: str) -> bool:
        return self.rows.pop(name, None) is not None


def store(repository: InMemorySecretRepository, key: bytes, **kwargs) -> EncryptedCredentialStore:
    return EncryptedCredentialStore(repository, Envelope(key), **kwargs)  # type: ignore[arg-type]


def a_key() -> bytes:
    return bytes(range(32))


# --- The master key -----------------------------------------------------------


def test_the_environment_is_where_a_deployment_says_what_the_key_is(tmp_path: Path) -> None:
    key = base64.urlsafe_b64encode(a_key()).decode("ascii")

    assert resolve_master_key(tmp_path, {MASTER_KEY_ENV: key}) == a_key()
    assert not (tmp_path / "master.key").exists(), "nothing is written when told the key"


def test_a_machine_that_was_told_nothing_gets_a_key_of_its_own(tmp_path: Path) -> None:
    """Refusing until configured is the ceremony that ends in plain text elsewhere."""
    first = resolve_master_key(tmp_path, {})
    path = tmp_path / "master.key"

    assert len(first) == 32
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert resolve_master_key(tmp_path, {}) == first, "and keeps it"


def test_a_key_that_is_the_wrong_shape_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        resolve_master_key(tmp_path, {MASTER_KEY_ENV: base64.urlsafe_b64encode(b"short").decode()})


def test_an_unreadable_key_file_is_not_treated_as_a_missing_one(tmp_path: Path) -> None:
    """Generating a new one here would make every stored credential unopenable."""
    (tmp_path / "master.key").write_text("not base64 at all!!", encoding="utf-8")

    with pytest.raises(StorageError):
        resolve_master_key(tmp_path, {})


# --- The envelope -------------------------------------------------------------


def test_what_reaches_the_store_does_not_contain_the_value() -> None:
    sealed, nonce = Envelope(a_key()).seal("openai_work", "sk-live-1234")

    assert b"sk-live" not in sealed
    assert Envelope(a_key()).open("openai_work", sealed, nonce) == "sk-live-1234"


def test_another_key_does_not_open_it() -> None:
    sealed, nonce = Envelope(a_key()).seal("openai_work", "sk-live-1234")

    with pytest.raises(StorageError):
        Envelope(bytes(32)).open("openai_work", sealed, nonce)


def test_a_ciphertext_moved_to_another_name_stops_decrypting() -> None:
    """The name is associated data, so a row swapped for another is not silently
    a different credential's value."""
    envelope = Envelope(a_key())
    sealed, nonce = envelope.seal("openai_work", "sk-live-1234")

    with pytest.raises(StorageError):
        envelope.open("openai_personal", sealed, nonce)


# --- The store ----------------------------------------------------------------


async def test_a_stored_credential_comes_back_after_a_restart() -> None:
    repository = InMemorySecretRepository()
    await store(repository, a_key()).store("openai_work", "sk-live-1234")

    fresh = store(repository, a_key())
    assert fresh.maybe("openai_work") is None, "nothing is answered before the rows are read"
    await fresh.restore()
    assert fresh.get("openai_work").reveal() == "sk-live-1234"


async def test_the_environment_wins_over_what_the_interface_stored() -> None:
    class Exported:
        def maybe(self, name: str) -> Secret | None:
            return Secret(name, "from-the-environment") if name == "openai_work" else None

        def get(self, name: str) -> Secret:
            raise SecretNotFoundError(name)

    repository = InMemorySecretRepository()
    credentials = store(repository, a_key(), fallback=Exported())
    await credentials.store("openai_work", "from-the-window")

    assert credentials.get("openai_work").reveal() == "from-the-environment"


async def test_a_forgotten_credential_is_gone_from_both_halves() -> None:
    repository = InMemorySecretRepository()
    credentials = store(repository, a_key())
    await credentials.store("openai_work", "sk-live-1234")

    assert await credentials.forget("openai_work")
    assert repository.rows == {}
    assert credentials.maybe("openai_work") is None
    assert not await credentials.forget("openai_work")


async def test_the_older_file_is_taken_over_once_and_never_erased() -> None:
    """An upgrade must not be something the user discovers by everything failing."""

    class OldFile:
        def __init__(self) -> None:
            self.read = 0

        def maybe(self, name: str) -> Secret | None:
            self.read += 1
            return Secret(name, "sk-from-the-file")

        def get(self, name: str) -> Secret:
            return Secret(name, "sk-from-the-file")

    old, repository = OldFile(), InMemorySecretRepository()
    credentials = store(repository, a_key())

    assert await credentials.import_from(old, ("telegram_bot_token",)) == 1
    assert credentials.get("telegram_bot_token").reveal() == "sk-from-the-file"
    assert await credentials.import_from(old, ("telegram_bot_token",)) == 0, "and not again"


# --- The operating system's vault (Phase 13) ------------------------------------------


class FakeKeyring:
    """Stands in for a `keyring` backend: a usable priority and a dict."""

    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_writes = False

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail_writes:
            raise RuntimeError("locked")
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class ForgetfulKeyring(FakeKeyring):
    """Accepts a write and keeps nothing - a vault that must not be trusted."""

    def set_password(self, service: str, account: str, value: str) -> None:
        return None


def _vault(tmp_path: Path, backend: FakeKeyring):
    from infrastructure.secrets.keychain import KeychainVault

    return KeychainVault(tmp_path, backend=backend)


def test_a_new_desktop_key_lives_only_in_the_vault(tmp_path: Path) -> None:
    backend = FakeKeyring()

    key = resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, backend))

    assert len(key) == 32
    assert not (tmp_path / "master.key").exists()
    assert resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, backend)) == key


def test_the_file_key_moves_into_the_vault_and_the_plaintext_is_gone(tmp_path: Path) -> None:
    original = resolve_master_key(tmp_path, {})  # an installation before Phase 13
    backend = FakeKeyring()

    moved = resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, backend))

    assert moved == original, "credentials sealed with it must still open"
    assert not (tmp_path / "master.key").exists()
    assert list(backend.values.values()) == [base64.urlsafe_b64encode(original).decode()]


def test_a_move_interrupted_after_the_vault_write_finishes_on_the_next_start(
    tmp_path: Path,
) -> None:
    original = resolve_master_key(tmp_path, {})
    backend = FakeKeyring()
    _vault(tmp_path, backend).write(original)  # crashed before the file was removed

    assert resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, backend)) == original
    assert not (tmp_path / "master.key").exists()


def test_two_different_keys_are_never_reconciled_by_guessing(tmp_path: Path) -> None:
    resolve_master_key(tmp_path, {})
    backend = FakeKeyring()
    _vault(tmp_path, backend).write(bytes(32))

    with pytest.raises(StorageError, match="differs"):
        resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, backend))
    assert (tmp_path / "master.key").exists(), "nothing was removed"


def test_a_vault_that_does_not_keep_the_key_leaves_the_file_where_it_is(tmp_path: Path) -> None:
    original = resolve_master_key(tmp_path, {})

    with pytest.raises(StorageError, match="did not keep"):
        resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, ForgetfulKeyring()))
    assert resolve_master_key(tmp_path, {}) == original


def test_a_locked_vault_is_a_refusal_with_what_to_do_not_a_file(tmp_path: Path) -> None:
    backend = FakeKeyring()
    backend.fail_writes = True

    with pytest.raises(StorageError, match="Unlock"):
        resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, backend))
    assert not (tmp_path / "master.key").exists()


def test_no_vault_on_this_machine_names_the_explicit_headless_choice(tmp_path: Path) -> None:
    class NoBackend(FakeKeyring):
        priority = 0

    with pytest.raises(ConfigurationError, match="PROMETHEUS_SECRET_BACKEND=file"):
        resolve_master_key(tmp_path, {}, vault=_vault(tmp_path, NoBackend()))
    assert not (tmp_path / "master.key").exists(), "no silent fallback to a file"


def test_two_data_directories_do_not_share_a_vault_entry(tmp_path: Path) -> None:
    backend = FakeKeyring()
    first = resolve_master_key(tmp_path / "a", {}, vault=_vault(tmp_path / "a", backend))
    second = resolve_master_key(tmp_path / "b", {}, vault=_vault(tmp_path / "b", backend))

    assert first != second
    assert len(backend.values) == 2


# --- The plaintext file from before Phase 17 -------------------------------------------


async def test_the_plaintext_credentials_file_is_removed_once_every_value_reads_back(
    tmp_path: Path,
) -> None:
    from infrastructure.secrets.local import LocalCredentialStore

    legacy = LocalCredentialStore(tmp_path / "credentials.json")
    await legacy.store("gmail_token", "tok-1")
    await legacy.store("notes_token", "tok-2")
    encrypted = store(InMemorySecretRepository(), a_key())
    names = await legacy.names()

    await encrypted.import_from(legacy, names)
    assert await encrypted.retire(legacy, names)

    assert not (tmp_path / "credentials.json").exists()
    assert encrypted.get("gmail_token").reveal() == "tok-1"


async def test_a_credential_changed_since_the_import_keeps_the_file(tmp_path: Path) -> None:
    from infrastructure.secrets.local import LocalCredentialStore

    legacy = LocalCredentialStore(tmp_path / "credentials.json")
    await legacy.store("gmail_token", "old")
    encrypted = store(InMemorySecretRepository(), a_key())
    await encrypted.store("gmail_token", "changed in the window")

    assert not await encrypted.retire(legacy, await legacy.names())
    assert (tmp_path / "credentials.json").exists()
