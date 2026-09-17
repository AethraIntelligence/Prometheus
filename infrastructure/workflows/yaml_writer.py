"""Writes a confirmed workflow draft as a declaration beside the others.

The only writer next to a registry that otherwise only reads, and deliberately
narrow: it creates a file that does not exist and refuses anything else. A draft
saved over an existing workflow would replace a process somebody wrote by hand
with one inferred from runs, and a name clash is a question for the person, not
a merge.

The file is written in the same shape `yaml_registry` reads and is read back
through it before it is kept, so a draft this build cannot load is never left
on disk as a declaration that breaks the next start.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from domain.errors import ConfigurationError
from domain.workflows.definition import WorkflowDefinition
from infrastructure.workflows.yaml_registry import DEFAULT_WORKFLOWS_DIR, _load

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class YamlWorkflowWriter:
    """Implements `domain.workflows.suggestion.WorkflowWriter`."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or DEFAULT_WORKFLOWS_DIR

    def write(self, definition: WorkflowDefinition) -> str:
        if not NAME.match(definition.name):
            raise ConfigurationError(
                "A workflow name is 2-63 lowercase letters, digits and hyphens."
            )
        self._directory.mkdir(parents=True, exist_ok=True)
        taken = {path.stem.split(".v")[0] for path in self._directory.glob("*.yaml")}
        target = self._directory / f"{definition.name}.yaml"
        header = (
            "# Saved from a workflow suggestion: a structure seen in several successful\n"
            "# runs, confirmed by a person. Manual, unscheduled, and granting nothing -\n"
            "# each step runs with its employee's own declaration.\n\n"
        )
        content = header + yaml.safe_dump(_document(definition), sort_keys=False)
        if definition.name in taken:
            # A process may stop after the file was made and before the
            # suggestion status was saved. Repeating the same confirmation is
            # recovery, not an overwrite; a different file remains protected.
            if target.exists() and target.read_text(encoding="utf-8") == content:
                return str(target)
            raise ConfigurationError(
                f"A workflow called '{definition.name}' already exists. Choose another name."
            )
        with target.open("x", encoding="utf-8") as handle:
            handle.write(content)
        try:
            _load(target)
        except ConfigurationError:
            target.unlink(missing_ok=True)
            raise
        return str(target)


def _document(definition: WorkflowDefinition) -> dict:
    snapshot = definition.to_snapshot()
    return {
        "name": snapshot["name"],
        "version": snapshot["version"],
        "description": snapshot["description"],
        "trigger": snapshot["trigger"],
        "inputs": {
            name: {
                "type": spec["kind"],
                "required": spec["required"],
                **({"default": spec["default"]} if spec["default"] is not None else {}),
                "description": spec["description"],
            }
            for name, spec in snapshot["input_schema"].items()
        },
        "steps": [
            {
                "name": step["name"],
                "employee": step["employee"],
                "instruction": step["instruction"],
                **({"depends_on": step["depends_on"]} if step["depends_on"] else {}),
            }
            for step in snapshot["steps"]
        ],
    }
