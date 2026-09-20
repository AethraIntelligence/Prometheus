"""The suite writes nothing into the data directory of whoever runs it.

Found the hard way: `Settings.data_dir` bound `_default_data_dir` as a
`default_factory` at class definition, so `tests/conftest.py` replacing that
function changed nothing. Every test that named no data directory then used
`~/.prometheus` - and one of them, building a credential store with the file
backend the suite chooses, wrote a second master key beside the real one. The
next start of the real runtime found two keys, could not tell which had sealed
the stored credentials, and refused to start. Nothing was lost and nothing
could be: the platform will not guess. But the machine was down until somebody
moved a file.

Two tests, because there are two ways in: this process, and a subprocess that
reads the environment.
"""

from __future__ import annotations

from pathlib import Path

from app.config.container import build_container
from app.config.settings import Settings, get_settings


def real_data_dir() -> Path:
    return Path.home() / ".prometheus"


def test_settings_that_name_no_data_directory_do_not_find_the_real_one() -> None:
    assert Settings().data_dir != real_data_dir()
    assert get_settings().data_dir != real_data_dir()


def test_a_container_built_with_nothing_configured_writes_no_key_beside_the_real_store() -> None:
    existing = (real_data_dir() / "master.key").exists()

    container = build_container()
    assert container.credential_store is not None

    assert (real_data_dir() / "master.key").exists() == existing, (
        "a test just wrote a master key into the data directory of whoever ran it"
    )
    assert container.settings.data_dir != real_data_dir()
