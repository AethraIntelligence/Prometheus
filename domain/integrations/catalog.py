"""Plugins: services this platform knows how to install, described as data.

Adding an MCP server by hand means knowing its package, its command, the name of
the variable its token goes in and what each of its tools does to the world -
four things a person who wants "GitHub" should not have to know. A plugin is
those four things written down once, beside an icon, so that installing one is
choosing it and pasting a token.

Three rules keep it inside the boundaries ADR 0015 drew.

**A plugin is a local declaration, not the server's claim.** The effect map on a
plugin was written here, by whoever added the file, from the tools the server
was seen to offer. It is exactly what a person classifying tools by hand would
have stored, and a tool it does not name is EXECUTE and asks - so a server that
grows a `delete_everything` in its next release gains a question, not a pass.

**Only the catalog says what runs.** An install names a plugin and supplies
values for the fields it declares. The command comes from the declaration and
never from the request, so the route that installs a plugin cannot be used to
start an arbitrary program; that is what "Custom server" is for, and it says so.

**A secret goes to the credential store, never onto the record.** A field of
kind SECRET is stored under its key and reaches the server through the child's
environment - or, where the server wants it as an argument, through a
`${KEY}` placeholder the transport fills in at start.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from domain.capabilities.models import Capability
from domain.employees.definition import EmployeeDefinition
from domain.errors import PluginConfigurationError
from domain.policies.risk import Effect

#: What an integration made from a plugin records, so the listing can tell a
#: plugin that is installed from one that merely shares a name.
PLUGIN_KEY = "plugin"

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PluginRuntime(StrEnum):
    """What has to be on this machine for the server to start."""

    NODE = "NODE"
    PYTHON = "PYTHON"
    DOCKER = "DOCKER"


class FieldKind(StrEnum):
    #: Stored encrypted, handed to the server in its environment, never shown.
    SECRET = "SECRET"
    #: A file or a directory on this machine.
    PATH = "PATH"
    TEXT = "TEXT"


@dataclass(frozen=True, slots=True)
class PluginField:
    """One thing a person supplies when installing.

    `key` is the environment variable a SECRET is handed over as, and the
    `${KEY}` an argument may name; for a PATH or TEXT it is only the latter.
    """

    key: str
    label: str
    kind: FieldKind = FieldKind.SECRET
    required: bool = True
    placeholder: str = ""
    help: str = ""
    #: Where to get it - the page that issues the token.
    help_url: str = ""


@dataclass(frozen=True, slots=True)
class PluginIcon:
    """A mark drawn from path data, so no image ever has to be loaded.

    Paths rather than an SVG document: the window's content security policy
    allows no image it did not ship, and markup from a file rendered as HTML is
    a way in that path data is not.
    """

    view_box: str
    paths: tuple[str, ...]
    color: str = "#111111"
    background: str = "#ffffff"


@dataclass(frozen=True, slots=True)
class SetupStep:
    """One thing to do before installing, where a token is not enough.

    Google is the case that needed it: a person creates their own OAuth client
    before there is anything to paste, and a sentence with a link beside the
    form is the difference between that taking ten minutes and not happening.
    """

    text: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class PluginSignIn:
    """How a plugin whose server signs in through the browser is asked to.

    A harmless read the server answers either with data - already signed in -
    or by opening its own sign-in page on this machine. The platform never
    reads that page or the reply: the tool succeeding is the only thing it
    takes as "signed in", so nothing depends on a server's wording.
    """

    label: str
    tool: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    help: str = ""


@dataclass(frozen=True, slots=True)
class Plugin:
    id: str
    name: str
    description: str
    category: str
    runtime: PluginRuntime
    command: str
    args: tuple[str, ...] = ()
    fields: tuple[PluginField, ...] = ()
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    effects: Mapping[str, Effect] = field(default_factory=dict)
    about: str = ""
    publisher: str = ""
    homepage: str = ""
    popular: bool = False
    icon: PluginIcon | None = None
    #: Fixed environment for the server - where it keeps its own tokens, say.
    #: Values may name a plain setting as `${KEY}`; never a secret's value.
    env: Mapping[str, str] = field(default_factory=dict)
    setup: tuple[SetupStep, ...] = ()
    sign_in: PluginSignIn | None = None

    def field_named(self, key: str) -> PluginField | None:
        return next((one for one in self.fields if one.key == key), None)


@dataclass(frozen=True, slots=True)
class Installation:
    """What installing a plugin writes: a record, and the secrets beside it."""

    configuration: dict[str, object]
    secret_names: tuple[str, ...]
    #: Key to value, for the credential store. Never put on the record.
    secrets: dict[str, str]


class PluginCatalog(Protocol):
    """Where plugins are declared. The only way anything learns one exists."""

    def list(self) -> list[Plugin]: ...

    def get(self, plugin_id: str) -> Plugin | None: ...


def plan_install(
    plugin: Plugin, values: Mapping[str, str], *, stored: frozenset[str] = frozenset()
) -> Installation:
    """Turn a plugin and a person's answers into what gets stored.

    Pure: nothing is written here. A required field left empty is refused with
    its label, and a value for a field the plugin does not declare is refused
    too - an unknown key would otherwise land in a server's environment, which
    is a way to set `NODE_OPTIONS` from a settings form.

    `stored` is the secrets this machine already keeps. A required one left
    empty is satisfied by it, so reinstalling after a removal does not ask for
    a token nobody has to hand any more; a value typed anyway replaces it.
    """
    unknown = sorted(set(values) - {one.key for one in plugin.fields})
    if unknown:
        raise PluginConfigurationError(
            f"{plugin.name} has no setting called {', '.join(unknown)}."
        )
    cleaned = {key: str(value).strip() for key, value in values.items()}
    for one in plugin.fields:
        satisfied = bool(cleaned.get(one.key)) or (
            one.kind is FieldKind.SECRET and one.key in stored
        )
        if one.required and not satisfied:
            raise PluginConfigurationError(f"{plugin.name} needs {one.label}.")

    plain = {
        one.key: cleaned[one.key]
        for one in plugin.fields
        if one.kind is not FieldKind.SECRET and cleaned.get(one.key)
    }
    secrets = {
        one.key: cleaned[one.key]
        for one in plugin.fields
        if one.kind is FieldKind.SECRET and cleaned.get(one.key)
    }
    args = [_fill(arg, plain) for arg in plugin.args]
    # An optional argument whose placeholder nobody filled is dropped whole
    # rather than passed as "--flag=${KEY}": the server's default is what an
    # empty optional field means.
    available = set(secrets) | {
        one.key for one in plugin.fields if one.kind is FieldKind.SECRET and one.key in stored
    }
    args = [arg for arg in args if not _unfilled_plain(arg, plugin, available)]
    # A plain setting no argument names reaches the server the way a secret
    # does, as its own variable: some servers read an address or an account
    # from the environment and take no flag for it.
    referenced = {key for arg in plugin.args for key in _PLACEHOLDER.findall(arg)}
    env = {key: _fill(value, plain) for key, value in plugin.env.items()}
    env.update({key: value for key, value in plain.items() if key not in referenced})
    configuration: dict[str, object] = {
        "command": plugin.command,
        "args": args,
        PLUGIN_KEY: plugin.id,
    }
    if env:
        configuration["env"] = env
    return Installation(
        configuration=configuration,
        secret_names=tuple(one.key for one in plugin.fields if one.kind is FieldKind.SECRET),
        secrets=secrets,
    )


def _fill(arg: str, plain: Mapping[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda match: plain.get(match.group(1), match.group(0)), arg)


def _unfilled_plain(arg: str, plugin: Plugin, secrets: Iterable[str]) -> bool:
    """An argument still naming a field nobody supplied a value for.

    A secret's placeholder is left for the transport, which fills it from the
    environment at start; anything else still unfilled here never will be.
    """
    for key in _PLACEHOLDER.findall(arg):
        declared = plugin.field_named(key)
        if declared is None:
            continue
        if declared.kind is FieldKind.SECRET:
            if key not in secrets:
                return True
            continue
        return True
    return False


def suggested_holders(
    plugin: Plugin, definitions: Iterable[EmployeeDefinition]
) -> list[str]:
    """Who should be granted a plugin nobody has chosen for yet.

    The employees already declaring a capability the plugin brings - the people
    whose work it extends - and everybody where none does, because a plugin
    granted to nobody is one nothing can use (ADR 0015), which a person who
    just installed it would read as broken.
    """
    enabled = [definition for definition in definitions if definition.enabled]
    matching = [
        definition.name
        for definition in enabled
        if plugin.capabilities & definition.capabilities
    ]
    return sorted(matching or [definition.name for definition in enabled])
