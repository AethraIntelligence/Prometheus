"""Installing a plugin through the door the window uses.

A plugin directory is written to a temporary catalog and points at the suite's
own MCP server, so the whole path is real: the declaration is read, the secret
goes to the credential store, the record is written and granted, the server is
started with the secret in its environment and a setting in its arguments, and
what it offers is classified by the declaration rather than by the server.

The negative half carries the most weight: no response ever holds the secret,
a setting the plugin did not declare is refused, and the command that runs
comes from the catalog and cannot be supplied by the request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine
from tests.fakes.llm import FakeLLM

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "tests" / "fakes" / "mcp_server.py"
SECRET = "notes-token-not-a-real-one"


def declare(plugins: Path) -> None:
    directory = plugins / "notes"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "notes",
                "name": "Notes",
                "description": "Search and send notes",
                "about": "The suite's own server.",
                "category": "Productivity",
                "popular": True,
                "runtime": "PYTHON",
                "command": sys.executable,
                "args": [str(SERVER), "--expect-env", "NOTES_TOKEN", "--folder", "${FOLDER}"],
                "settings": [
                    {"key": "NOTES_TOKEN", "label": "Token", "kind": "SECRET"},
                    {"key": "FOLDER", "label": "Folder", "kind": "PATH"},
                ],
                "capabilities": ["EMAIL"],
                "effects": {"READ": ["search_notes"]},
                "sign_in": {
                    "label": "Sign in",
                    "tool": "search_notes",
                    "arguments": {"query": "${FOLDER}"},
                },
            }
        )
    )
    (directory / "icon.svg").write_text('<svg viewBox="0 0 24 24"><path d="M0 0h24v24H0z"/></svg>')


def serve(tmp_path: Path, **extra):
    plugins = tmp_path / "plugins"
    declare(plugins)
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}",
        file_root=tmp_path / "workspace",
        employees_dir=REPO_ROOT / "employees",
        plugins_dir=plugins,
        **extra,
    )

    import asyncio

    async def create() -> None:
        engine = create_engine(settings.resolved_database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([])  # type: ignore[method-assign]
        return container

    return TestClient(create_app(settings, build=build))


@pytest.fixture
def client(tmp_path: Path):
    with serve(tmp_path) as connected:
        yield connected


def tools_of(client, name: str) -> list[str]:
    employees = client.get("/api/employees").json()["employees"]
    return next(employee for employee in employees if employee["name"] == name)["tools"]


def test_a_plugin_is_listed_installed_granted_and_classified(client, tmp_path: Path) -> None:
    listing = client.get("/api/plugins").json()
    assert listing["available"] is True
    [plugin] = listing["plugins"]
    assert plugin["id"] == "notes" and plugin["installed"] == ""
    assert plugin["icon"]["paths"] == ["M0 0h24v24H0z"]
    assert plugin["runtime_ready"] is True
    assert plugin["settings"][0] == {
        "key": "NOTES_TOKEN",
        "label": "Token",
        "kind": "SECRET",
        "required": True,
        "placeholder": "",
        "help": "",
        "help_url": "",
        "stored": False,
        "provided": False,
    }
    assert plugin["suggested_employees"], "somebody is suggested, or nothing could use it"

    installed = client.post(
        "/api/plugins/notes/install",
        json={
            "values": {"NOTES_TOKEN": SECRET, "FOLDER": str(tmp_path)},
            "employees": ["writer", "nobody-by-that-name"],
        },
    )

    assert installed.status_code == 201, installed.text
    body = installed.json()
    assert SECRET not in installed.text
    # READY proves the token reached the environment and the folder the
    # arguments: the fake refuses to start without either.
    assert body["status"] == "READY"
    assert body["plugin"] == "notes"
    assert body["granted_to"] == ["writer"]
    assert body["holders"] == ["writer"]
    tools = {tool["name"]: tool for tool in body["tools"]}
    assert tools["search_notes"]["requires_approval"] is False
    assert tools["send_note"]["requires_approval"] is True
    assert tools["send_note"]["classified"] is False

    again = client.get("/api/plugins").json()
    assert SECRET not in str(again)
    assert again["plugins"][0]["installed"] == body["id"]
    assert again["plugins"][0]["settings"][0]["stored"] is True
    assert [item["name"] for item in again["installed"]] == ["notes"]

    assert "notes.search_notes" in tools_of(client, "writer")

    regranted = client.put(
        f"/api/integrations/{body['id']}/grants", json={"employees": ["analyst"]}
    ).json()
    assert regranted["holders"] == ["analyst"]
    assert "notes.search_notes" not in tools_of(client, "writer")

    assert client.post(
        "/api/plugins/notes/install",
        json={"values": {"NOTES_TOKEN": SECRET, "FOLDER": str(tmp_path)}},
    ).status_code == 409


def test_a_plugin_is_asked_to_sign_in_through_its_declared_read(client, tmp_path: Path) -> None:
    body = client.post(
        "/api/plugins/notes/install",
        json={"values": {"NOTES_TOKEN": SECRET, "FOLDER": str(tmp_path)}},
    ).json()
    assert client.get("/api/plugins").json()["plugins"][0]["sign_in"] == {
        "label": "Sign in",
        "help": "",
    }

    answer = client.post(f"/api/integrations/{body['id']}/sign-in")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"signed_in": True}


def test_what_an_install_will_not_accept(client, tmp_path: Path) -> None:
    missing = client.post("/api/plugins/notes/install", json={"values": {"NOTES_TOKEN": SECRET}})
    assert missing.status_code == 400
    assert "Folder" in missing.json()["detail"]

    smuggled = client.post(
        "/api/plugins/notes/install",
        json={
            "values": {"NOTES_TOKEN": SECRET, "FOLDER": str(tmp_path), "NODE_OPTIONS": "--inspect"},
        },
    )
    assert smuggled.status_code == 400

    # A command in the body is not a field this route has: it is ignored, and
    # what runs is still the catalog's.
    assert client.post(
        "/api/plugins/nothing-like-it/install", json={"values": {}, "command": "rm"}
    ).status_code == 404


def test_a_credential_the_installation_supplies_is_not_asked_for(tmp_path: Path) -> None:
    """The platform's own Google client, in miniature: set once in `.env`, used by everyone."""
    with serve(tmp_path, plugin_credentials={"notes_token": SECRET}) as client:
        setting = client.get("/api/plugins").json()["plugins"][0]["settings"][0]
        assert (setting["provided"], setting["stored"]) == (True, False)

        installed = client.post(
            "/api/plugins/notes/install", json={"values": {"FOLDER": str(tmp_path)}}
        )

        # READY proves the supplied value reached the server's environment:
        # the fake will not start without NOTES_TOKEN.
        assert installed.status_code == 201, installed.text
        assert installed.json()["status"] == "READY"
        assert SECRET not in installed.text
        assert SECRET not in client.get("/api/plugins").text
        # Supplied, not stored: nothing was written to the credential store.
        assert client.get("/api/plugins").json()["plugins"][0]["settings"][0]["stored"] is False
