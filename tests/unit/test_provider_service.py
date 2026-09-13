"""Adding a provider, a key and a model, and saying where the work goes.

The four rules defended here are the ones a settings page would otherwise get
right or wrong per surface: a key goes in and never comes back, a kind nothing
implements is refused, a connection models depend on cannot be deleted, and work
can only be sent to a model that exists.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application.providers.service import ProviderService
from domain.capabilities.models import Capability
from domain.errors import ConfigurationError, NotFoundError
from domain.llm.catalog import ModelEntry
from domain.llm.models import TaskKind
from domain.providers.models import Connection, InstalledModels
from domain.secrets.models import Secret
from domain.workspace.models import DEFAULT_WORKSPACE_ID


@dataclass(frozen=True)
class Kind:
    name: str
    label: str = ""
    needs_credential: bool = True
    default_base_url: str = ""


KINDS = (Kind("openai", "OpenAI"), Kind("local", "Local", False, "http://127.0.0.1:11434/v1"))


class InMemoryConnections:
    def __init__(self) -> None:
        self.rows: dict[str, Connection] = {}

    async def save(self, connection: Connection) -> None:
        self.rows[connection.name] = connection

    async def get(self, connection_id):  # pragma: no cover - unused here
        return next((c for c in self.rows.values() if c.id == connection_id), None)

    async def get_by_name(self, name, workspace_id=DEFAULT_WORKSPACE_ID):
        return self.rows.get(name)

    async def list(self, workspace_id=DEFAULT_WORKSPACE_ID):
        return sorted(self.rows.values(), key=lambda c: c.name)

    async def delete(self, connection_id) -> bool:
        for name, connection in list(self.rows.items()):
            if connection.id == connection_id:
                del self.rows[name]
                return True
        return False


class InMemoryCatalog:
    def __init__(self) -> None:
        self.rows: dict[str, ModelEntry] = {}
        self.chosen: dict[TaskKind, str] = {}

    async def save_entry(self, entry, workspace_id=DEFAULT_WORKSPACE_ID) -> None:
        self.rows[entry.name] = entry

    async def entries(self, workspace_id=DEFAULT_WORKSPACE_ID):
        return list(self.rows.values())

    async def delete_entry(self, name, workspace_id=DEFAULT_WORKSPACE_ID) -> bool:
        return self.rows.pop(name, None) is not None

    async def entries_using(self, connection, workspace_id=DEFAULT_WORKSPACE_ID):
        return [entry for entry in self.rows.values() if entry.connection == connection]

    async def set_default(self, task_kind, entry_name, workspace_id=DEFAULT_WORKSPACE_ID) -> None:
        self.chosen[task_kind] = entry_name

    async def defaults(self, workspace_id=DEFAULT_WORKSPACE_ID):
        return dict(self.chosen)

    async def clear_default(self, task_kind) -> bool:
        return self.chosen.pop(task_kind, None) is not None


class RecordingCredentials:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}
        self.forgotten: list[str] = []

    async def store(self, name: str, value: str) -> None:
        self.stored[name] = value

    async def forget(self, name: str) -> bool:
        self.forgotten.append(name)
        return self.stored.pop(name, None) is not None

    async def names(self) -> tuple[str, ...]:
        return tuple(self.stored)

    def maybe(self, name: str) -> Secret | None:
        value = self.stored.get(name)
        return Secret(name, value) if value else None


def service(**extra) -> tuple[ProviderService, InMemoryConnections, InMemoryCatalog,
                              RecordingCredentials]:
    connections, catalog, credentials = (
        InMemoryConnections(), InMemoryCatalog(), RecordingCredentials()
    )
    return (
        ProviderService(connections, catalog, credentials=credentials, kinds=KINDS, **extra),
        connections,
        catalog,
        credentials,
    )


# --- Connections --------------------------------------------------------------


async def test_two_accounts_on_one_provider_are_two_connections() -> None:
    providers, connections, _, credentials = service()

    await providers.add_connection("openai-work", "openai", api_key="sk-work")
    await providers.add_connection("openai-personal", "openai", api_key="sk-home")

    assert [c.name for c in await providers.list_connections()] == [
        "openai-personal",
        "openai-work",
    ]
    assert set(credentials.stored.values()) == {"sk-work", "sk-home"}
    assert all("sk-" not in str(c) for c in connections.rows.values())


async def test_the_key_is_stored_where_keys_go_and_the_row_only_names_it() -> None:
    providers, connections, _, credentials = service()

    connection = await providers.add_connection("openai-work", "openai", api_key="sk-live")

    assert connection.secret_name == "openai_work_api_key"
    assert credentials.stored["openai_work_api_key"] == "sk-live"
    assert "sk-live" not in repr(connections.rows["openai-work"])


async def test_a_kind_this_machine_cannot_talk_to_is_refused() -> None:
    """It would look configured in the window and fail at the first call."""
    providers, _, _, _ = service()

    with pytest.raises(ConfigurationError) as error:
        await providers.add_connection("something", "some-startup", api_key="k")

    assert "openai" in str(error.value)


async def test_a_local_runner_needs_no_key_and_a_hosted_one_does() -> None:
    providers, _, _, _ = service()

    local = await providers.add_connection("ollama", "local")
    assert local.base_url == "http://127.0.0.1:11434/v1", "the kind's own default address"

    with pytest.raises(ConfigurationError):
        await providers.add_connection("openai-work", "openai")


async def test_two_connections_cannot_share_a_name() -> None:
    """The name is what a model says to choose an account with."""
    providers, _, _, _ = service()
    await providers.add_connection("openai-work", "openai", api_key="sk-one")

    with pytest.raises(ConfigurationError):
        await providers.add_connection("openai-work", "openai", api_key="sk-two")


async def test_rotating_a_key_leaves_everything_pointing_at_it_alone() -> None:
    providers, _, _, credentials = service()
    before = await providers.add_connection("openai-work", "openai", api_key="sk-old")

    after = await providers.replace_key("openai-work", "sk-new")

    assert credentials.stored[after.secret_name] == "sk-new"
    assert after.id == before.id and after.name == before.name


async def test_a_connection_models_depend_on_is_not_quietly_removed() -> None:
    providers, _, catalog, _ = service()
    await providers.add_connection("openai-work", "openai", api_key="sk-live")
    await providers.add_model(
        ModelEntry(name="work-model", provider="openai", model="m", connection="openai-work")
    )

    with pytest.raises(ConfigurationError) as error:
        await providers.remove_connection("openai-work")

    assert "work-model" in str(error.value), "and says which model is holding it"
    assert catalog.rows, "nothing was removed"


async def test_removing_a_connection_takes_its_credential_with_it() -> None:
    providers, _, _, credentials = service()
    await providers.add_connection("openai-work", "openai", api_key="sk-live")

    await providers.remove_connection("openai-work")

    assert credentials.stored == {}
    assert credentials.forgotten == ["openai_work_api_key"]


# --- Models -------------------------------------------------------------------


async def test_a_model_can_only_name_a_connection_that_exists() -> None:
    providers, _, _, _ = service()

    with pytest.raises(NotFoundError):
        await providers.add_model(
            ModelEntry(name="m", provider="openai", model="x", connection="nowhere")
        )


async def test_what_the_runner_already_has_is_offered_rather_than_typed() -> None:
    """A field where a person types a model name accepts a typo silently."""

    async def discover(kind: str, base_url: str) -> InstalledModels:
        assert (kind, base_url) == ("local", "http://127.0.0.1:11434/v1")
        return InstalledModels(("gemma4:31b-cloud", "lfm2:24b"), supported=True, reachable=True)

    providers, _, _, _ = service(discover=discover)
    await providers.add_connection("ollama", "local")

    found = await providers.available_models("ollama")
    assert found.names == ("gemma4:31b-cloud", "lfm2:24b")


async def test_a_machine_that_cannot_discover_says_the_kind_cannot_be_asked() -> None:
    providers, _, _, _ = service()
    await providers.add_connection("ollama", "local")

    assert await providers.available_models("ollama") == InstalledModels()


# --- Where work goes ----------------------------------------------------------


async def test_work_is_given_to_a_model_by_name() -> None:
    """The request this whole phase came from, in one call."""
    providers, _, catalog, _ = service()
    await providers.add_connection("openai-work", "openai", api_key="sk-live")
    await providers.add_model(
        ModelEntry(
            name="work-model",
            provider="openai",
            model="m",
            connection="openai-work",
            capabilities=frozenset({Capability.TOOL_CALLING}),
        )
    )

    await providers.send_work_to(TaskKind.PLANNING, "work-model")

    assert catalog.chosen[TaskKind.PLANNING] == "work-model"
    assert (await providers.defaults())[TaskKind.PLANNING] == "work-model"


async def test_work_cannot_be_sent_to_a_model_that_does_not_exist() -> None:
    """The router treats a default as the winner, so a dangling one is an outage."""
    providers, _, _, _ = service()

    with pytest.raises(NotFoundError):
        await providers.send_work_to(TaskKind.PLANNING, "imaginary")


async def test_every_change_tells_the_running_process() -> None:
    """A settings page whose effect begins after a restart stops being trusted."""
    reloads = []

    providers, _, _, _ = service(on_change=lambda: reloads.append(1))
    await providers.add_connection("ollama", "local")
    await providers.add_model(ModelEntry(name="m", provider="local", model="x"))
    await providers.send_work_to(TaskKind.PLANNING, "m")

    assert len(reloads) == 3


async def test_removing_a_model_un_routes_the_work_that_went_to_it() -> None:
    """A default naming a missing entry is ignored when the catalog loads, so
    the platform behaves - but the row stayed and every page showed a routing
    that was not in force."""
    providers, _, catalog, _ = service()
    await providers.add_model(ModelEntry(name="m", provider="local", model="x"))
    await providers.send_work_to(TaskKind.PLANNING, "m")

    await providers.remove_model("m")

    assert catalog.chosen == {}
