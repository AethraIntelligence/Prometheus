"""Whether this machine can start a plugin's server at all.

Asked before a person fills in a form, so "you need Node.js first" arrives as a
sentence on the plugin rather than as "could not start 'npx'" after they pasted
a token. Found the same way the transport finds a program, so the answer here
and the behaviour at start cannot disagree.
"""

from __future__ import annotations

from domain.integrations.catalog import PluginRuntime
from infrastructure.integrations import programs

PROGRAMS: dict[PluginRuntime, str] = {
    PluginRuntime.NODE: "npx",
    PluginRuntime.PYTHON: "uvx",
    PluginRuntime.DOCKER: "docker",
}

#: What to install, in words and with where to get it.
HINTS: dict[PluginRuntime, tuple[str, str]] = {
    PluginRuntime.NODE: ("Needs Node.js 20 or later.", "https://nodejs.org/en/download"),
    PluginRuntime.PYTHON: ("Needs uv.", "https://docs.astral.sh/uv/getting-started/installation/"),
    PluginRuntime.DOCKER: (
        "Needs Docker Desktop, running.",
        "https://www.docker.com/products/docker-desktop/",
    ),
}


def available() -> dict[PluginRuntime, bool]:
    return {runtime: programs.find(program) is not None for runtime, program in PROGRAMS.items()}
