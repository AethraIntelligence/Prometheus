"""Settings -> General: what a window saves is what the next start reads.

Against a real `Settings` and a real file in `tmp_path`, because the property
worth having is the round trip - saved by the editor, read back by the settings
source - and a test of either half alone would agree with a file nobody reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config.editable import EDITABLE
from app.config.general import FileSettingsEditor
from app.config.settings import Settings
from domain.configuration.models import SettingKind
from domain.errors import SettingValueError


def editor(tmp_path: Path, **environ: str) -> FileSettingsEditor:
    return FileSettingsEditor(Settings(data_dir=tmp_path), environ=environ)


def by_key(settings) -> dict:
    return {item.key: item for item in settings}


def test_every_listed_setting_is_a_real_field_with_its_running_value(tmp_path: Path) -> None:
    listed = by_key(editor(tmp_path).current())

    assert set(listed) == {entry.key for entry in EDITABLE}
    assert listed["flags.memory"].value is True
    assert listed["approval_mode"].choices == ("prompt", "deny", "allow")
    assert not any(item.restart_needed for item in listed.values())


def test_a_change_is_saved_for_the_next_start_and_says_so(tmp_path: Path) -> None:
    changed = by_key(
        editor(tmp_path).change(
            {
                "flags.scheduler": True,
                "approval_mode": "deny",
                "computer_allowed_applications": ["Preview", " ", "Calculator"],
            }
        )
    )

    assert changed["flags.scheduler"].value is True
    assert changed["flags.scheduler"].running is False
    assert changed["flags.scheduler"].restart_needed
    assert changed["computer_allowed_applications"].value == ("Preview", "Calculator")

    next_start = Settings(data_dir=tmp_path)
    assert next_start.scheduler_enabled is True
    assert next_start.approval_mode == "deny"
    assert next_start.computer_allowed_applications == ("Preview", "Calculator")
    # Everything not saved keeps its default rather than being written down.
    assert next_start.flags.memory is True


def test_a_saved_file_outranks_nothing_the_shell_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"flags": {"memory": False, "workflows": False}}), encoding="utf-8"
    )
    monkeypatch.setenv("PROMETHEUS_FLAGS__MEMORY", "true")

    resolved = Settings(data_dir=tmp_path)

    assert resolved.flags.memory is True
    assert resolved.flags.workflows is False


def test_a_setting_the_environment_decides_is_shown_locked_and_refused(tmp_path: Path) -> None:
    locked = editor(tmp_path, PROMETHEUS_FLAGS__MEMORY="false")

    assert by_key(locked.current())["flags.memory"].locked_by == "PROMETHEUS_FLAGS__MEMORY"
    with pytest.raises(SettingValueError, match="PROMETHEUS_FLAGS__MEMORY"):
        locked.change({"flags.memory": True})


@pytest.mark.parametrize(
    "values",
    [
        {"database_url": "postgresql://elsewhere/db"},
        {"approval_mode": "sometimes"},
        {"flags.memory": "yes"},
        {"computer_max_actions": 0},
        {"memory_recall_limit": True},
        {"response_language": "  "},
    ],
)
def test_a_value_the_setting_does_not_take_saves_nothing(tmp_path: Path, values: dict) -> None:
    subject = editor(tmp_path)

    with pytest.raises(SettingValueError):
        subject.change({"flags.scheduler": True, **values})

    assert not (tmp_path / "settings.json").exists()


def test_an_optional_value_can_be_cleared(tmp_path: Path) -> None:
    subject = editor(tmp_path)
    subject.change({"llm_timeout_seconds": 300})
    cleared = by_key(subject.change({"llm_timeout_seconds": ""}))

    assert cleared["llm_timeout_seconds"].value is None
    assert Settings(data_dir=tmp_path).llm_timeout_seconds is None


def test_a_hand_edited_file_cannot_reach_what_the_window_does_not_list(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"database_url": "postgresql://elsewhere/db", "log_format": "console"}),
        encoding="utf-8",
    )

    resolved = Settings(data_dir=tmp_path)

    assert resolved.database_url is None
    assert resolved.log_format == "console"


def test_a_damaged_file_does_not_stop_the_platform_starting(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")

    assert Settings(data_dir=tmp_path).approval_mode == "prompt"
    assert editor(tmp_path).change({"log_level": "DEBUG"})
    assert Settings(data_dir=tmp_path).log_level == "DEBUG"


def test_every_kind_is_one_the_window_knows_how_to_draw() -> None:
    assert {entry.kind for entry in EDITABLE} <= set(SettingKind)
    assert all(entry.choices for entry in EDITABLE if entry.kind is SettingKind.CHOICE)


def test_resetting_one_setting_forgets_it_and_owes_a_restart(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"approval_mode": "deny", "flags": {"scheduler": True}}), encoding="utf-8"
    )
    subject = editor(tmp_path)
    before = by_key(subject.current())
    assert before["approval_mode"].saved
    assert before["approval_mode"].default == "prompt"

    after = by_key(subject.reset(["approval_mode"]))

    assert not after["approval_mode"].saved
    assert after["approval_mode"].value == "prompt"
    assert after["approval_mode"].running == "deny"
    assert after["approval_mode"].restart_needed
    assert after["flags.scheduler"].saved
    assert Settings(data_dir=tmp_path).approval_mode == "prompt"


def test_resetting_everything_falls_back_to_the_env_file_not_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PROMETHEUS_LOG_FORMAT=console\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    subject = editor(tmp_path)
    subject.change({"log_format": "json", "flags.memory": False})

    after = by_key(subject.reset())

    assert after["log_format"].default == "console"
    assert not any(item.saved for item in after.values())
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {}
    assert Settings(data_dir=tmp_path).log_format == "console"


def test_resetting_an_unknown_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingValueError):
        editor(tmp_path).reset(["database_url"])
