"""Plugins: every shipped declaration loads, and installing one is decided in the domain.

The catalog is a shop window that skips a broken tile at runtime, so this is
where a broken tile fails the build instead: every directory under `plugins/`
is loaded strictly. The rest pins the rules an install is held to - which
values it accepts, where a secret goes, who is granted it by default - and that
an icon file is a drawing and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.capabilities.models import Capability
from domain.errors import ConfigurationError, PluginConfigurationError
from domain.integrations.catalog import (
    FieldKind,
    Plugin,
    PluginField,
    PluginRuntime,
    plan_install,
    suggested_holders,
)
from domain.policies.risk import Effect
from infrastructure.integrations.yaml_catalog import YamlPluginCatalog, load
from tests.fakes.employees import definition

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_shipped_plugin_loads_strictly() -> None:
    catalog = YamlPluginCatalog(REPO_ROOT / "plugins", strict=True)
    plugins = catalog.list()

    assert len(plugins) >= 20
    for plugin in plugins:
        assert plugin.icon is not None, f"{plugin.id} has no icon"
        assert plugin.description and plugin.about, plugin.id


def test_a_shipped_plugin_never_calls_an_unclassified_tool_safe() -> None:
    """Nothing that sends, publishes, deletes or spends is waved through by a catalog.

    A plugin may mark reads and local writes. Anything else is left out, so it
    is EXECUTE and asks - the direction the two ways of being wrong point.
    """
    allowed = {Effect.READ, Effect.WRITE}
    for plugin in YamlPluginCatalog(REPO_ROOT / "plugins", strict=True).list():
        loud = {name for name, effect in plugin.effects.items() if effect not in allowed}
        # Sending is declared as SEND where it is known, which asks every time;
        # it is listed here so a new entry has to be looked at, not waved on.
        declared_sends = {"slack": {"conversations_add_message"}, "gmail": {"send_gmail_message"}}
        if plugin.id in declared_sends:
            assert loud == declared_sends[plugin.id]
            assert all(plugin.effects[name] is Effect.SEND for name in loud)
            continue
        assert not loud, f"{plugin.id} classifies {loud} as something that does not ask"


def notes(**extra) -> Plugin:
    fields = extra.pop(
        "fields",
        (
            PluginField("NOTES_TOKEN", "Token"),
            PluginField("FOLDER", "Folder", kind=FieldKind.PATH),
            PluginField("REGION", "Region", kind=FieldKind.TEXT, required=False),
        ),
    )
    capabilities = extra.pop("capabilities", frozenset({Capability.FILE_ACCESS}))
    return Plugin(
        id="notes",
        name="Notes",
        description="Notes",
        category="Productivity",
        runtime=PluginRuntime.NODE,
        command="npx",
        args=("-y", "notes-server", "${FOLDER}", "--region=${REGION}", "--db=${NOTES_TOKEN}"),
        fields=fields,
        capabilities=capabilities,
        **extra,
    )


def test_an_install_fills_plain_settings_and_leaves_secrets_to_the_environment() -> None:
    plan = plan_install(notes(), {"NOTES_TOKEN": " secret ", "FOLDER": "/work"})

    assert plan.configuration == {
        "command": "npx",
        # REGION was optional and left empty, so its argument is gone rather
        # than passed as a literal placeholder; the secret stays a placeholder.
        "args": ["-y", "notes-server", "/work", "--db=${NOTES_TOKEN}"],
        "plugin": "notes",
    }
    assert plan.secrets == {"NOTES_TOKEN": "secret"}
    assert plan.secret_names == ("NOTES_TOKEN",)
    assert "secret" not in str(plan.configuration)


def test_a_plain_setting_no_argument_names_reaches_the_server_as_its_own_variable() -> None:
    plugin = notes(
        fields=(
            PluginField("CLIENT_ID", "Client"),
            PluginField("ACCOUNT", "Account", kind=FieldKind.TEXT),
        ),
        env={"TOKENS_DIR": "~/.tokens/${ACCOUNT}"},
    )

    plan = plan_install(plugin, {"CLIENT_ID": "id", "ACCOUNT": "me@example.com"})

    assert plan.configuration["env"] == {
        "TOKENS_DIR": "~/.tokens/me@example.com",
        "ACCOUNT": "me@example.com",
    }
    assert "id" not in str(plan.configuration), "a secret is never on the record"


def test_a_plugin_that_signs_in_through_the_browser_declares_how_to_ask() -> None:
    catalog = YamlPluginCatalog(REPO_ROOT / "plugins", strict=True)
    for plugin_id in ("gmail", "google-drive"):
        plugin = catalog.get(plugin_id)
        assert plugin is not None and plugin.sign_in is not None
        assert plugin.effects[plugin.sign_in.tool] is Effect.READ, "the probe must be a read"
        assert plugin.setup, "Google needs its own client, and the steps say how"
        assert plugin.env["WORKSPACE_MCP_CREDENTIALS_DIR"].endswith(plugin_id.split("-")[-1])


def test_a_required_setting_left_empty_is_refused_by_its_label() -> None:
    with pytest.raises(PluginConfigurationError, match="Folder"):
        plan_install(notes(), {"NOTES_TOKEN": "x", "FOLDER": "  "})


def test_a_secret_this_machine_already_keeps_need_not_be_typed_again() -> None:
    plan = plan_install(notes(), {"FOLDER": "/work"}, stored=frozenset({"NOTES_TOKEN"}))

    assert plan.secrets == {}
    assert "--db=${NOTES_TOKEN}" in plan.configuration["args"]


def test_a_value_for_a_setting_the_plugin_does_not_declare_is_refused() -> None:
    """Otherwise a form could put NODE_OPTIONS into a server's environment."""
    with pytest.raises(PluginConfigurationError, match="NODE_OPTIONS"):
        plan_install(notes(), {"NOTES_TOKEN": "x", "FOLDER": "/w", "NODE_OPTIONS": "--inspect"})


def test_a_plugin_goes_to_whose_work_it_extends_and_else_to_everyone() -> None:
    reader = definition("reader", capabilities=frozenset({Capability.FILE_ACCESS}))
    coder = definition("coder", capabilities=frozenset({Capability.CODE}))

    assert suggested_holders(notes(), [reader, coder]) == ["reader"]
    assert suggested_holders(notes(capabilities=frozenset()), [reader, coder]) == [
        "coder",
        "reader",
    ]


def write(directory: Path, yaml_text: str, svg: str | None = None) -> Path:
    directory.mkdir(parents=True)
    path = directory / "plugin.yaml"
    path.write_text(yaml_text)
    if svg is not None:
        (directory / "icon.svg").write_text(svg)
    return path


MINIMAL = """
id: sample
name: Sample
description: A sample
category: Utilities
runtime: python
command: uvx
args: [sample-server]
effects:
  READ: [look]
"""


def test_an_icon_keeps_path_data_and_drops_everything_else(tmp_path: Path) -> None:
    path = write(
        tmp_path / "sample",
        MINIMAL,
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<script>alert(1)</script><path d="M0 0h24v24H0z"/><a href="x"/></svg>',
    )

    plugin = load(path)

    assert plugin.icon is not None
    assert plugin.icon.paths == ("M0 0h24v24H0z",)
    assert plugin.effects == {"look": Effect.READ}


def test_path_data_that_is_not_drawing_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path / "sample",
        MINIMAL,
        '<svg viewBox="0 0 24 24"><path d="javascript:alert(1)"/></svg>',
    )

    with pytest.raises(ConfigurationError, match="path data"):
        load(path)


def test_a_plugin_whose_id_is_not_its_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="directory"):
        load(write(tmp_path / "other", MINIMAL))


def test_a_broken_plugin_is_skipped_at_runtime_and_named_in_strict_mode(tmp_path: Path) -> None:
    write(tmp_path / "sample", MINIMAL)
    write(tmp_path / "broken", "id: broken\nname: Broken\n")

    assert [plugin.id for plugin in YamlPluginCatalog(tmp_path).list()] == ["sample"]
    with pytest.raises(ConfigurationError, match="broken"):
        YamlPluginCatalog(tmp_path, strict=True).list()
