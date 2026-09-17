"""Pin every shipped plugin to an exact artifact and lock what was reviewed.

For each `plugins/<id>/plugin.yaml` this finds the package its command fetches,
pins it in the declaration's arguments (`name@version`, `image@sha256:...`),
asks the registry for the digest it publishes for exactly that version, and
writes `plugins/catalog.lock.json` with that digest and a digest of the
declaration's files. The catalog refuses any plugin the lock does not describe
(`domain/integrations/provenance.py`).

Run it deliberately, as a reviewed change: a new lock is a statement that the
new versions were looked at.

    uv run python scripts/release/lock_plugins.py            # keep existing pins
    uv run python scripts/release/lock_plugins.py --upgrade  # move to the latest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from domain.integrations.provenance import (  # noqa: E402
    ArtifactKind,
    PluginArtifact,
    declaration_digest,
)
from infrastructure.integrations.provenance import RegistryArtifactVerifier  # noqa: E402

PLUGINS = REPO / "plugins"
LOCK = PLUGINS / "catalog.lock.json"
IMAGE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?/[a-z0-9._/-]+")


def artifact_argument(runtime: str, args: list[str]) -> int:
    if runtime == "DOCKER":
        for index in range(len(args) - 1, -1, -1):
            if IMAGE.match(args[index]):
                return index
        raise SystemExit(f"no image in {args}")
    for index, arg in enumerate(args):
        if not arg.startswith("-"):
            return index
    raise SystemExit(f"no package in {args}")


def split(spec: str, kind: ArtifactKind) -> tuple[str, str]:
    if kind is ArtifactKind.OCI:
        name, _, digest = spec.partition("@")
        return name.split(":")[0], digest
    at = spec.rfind("@")
    if at > 0:
        return spec[:at], spec[at + 1 :]
    return spec, ""


def latest(client: httpx.Client, kind: ArtifactKind, name: str) -> str:
    if kind is ArtifactKind.NPM:
        return client.get(f"https://registry.npmjs.org/{name}/latest").json()["version"]
    if kind is ArtifactKind.PYPI:
        return client.get(f"https://pypi.org/pypi/{name}/json").json()["info"]["version"]
    registry, _, repository = name.partition("/")
    token = client.get(
        f"https://{registry}/token",
        params={"scope": f"repository:{repository}:pull", "service": registry},
    ).json()["token"]
    answer = client.head(
        f"https://{registry}/v2/{repository}/manifests/latest",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": RegistryArtifactVerifier.OCI_ACCEPT,
        },
    )
    return answer.headers["docker-content-digest"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true")
    arguments = parser.parse_args()
    kinds = {"NODE": ArtifactKind.NPM, "PYTHON": ArtifactKind.PYPI, "DOCKER": ArtifactKind.OCI}
    verifier = RegistryArtifactVerifier()
    lock: dict[str, dict] = {}

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for path in sorted(PLUGINS.glob("*/plugin.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            kind = kinds[raw["runtime"]]
            args = [str(arg) for arg in raw.get("args") or []]
            index = artifact_argument(raw["runtime"], args)
            name, version = split(args[index], kind)
            if arguments.upgrade or not version:
                version = latest(client, kind, name)
            reference = f"{name}@{version}"
            if args[index] != reference:
                text = path.read_text(encoding="utf-8")
                old = args[index]
                pattern = re.compile(rf"^- '?{re.escape(old)}'?$", re.MULTILINE)
                replaced, count = pattern.subn(f"- '{reference}'", text, count=1)
                if count != 1:
                    raise SystemExit(f"{path}: could not pin {old}")
                path.write_text(replaced, encoding="utf-8")
            artifact = PluginArtifact(kind, name, version, ())
            published = verifier.published_sync(client, artifact)
            files = [
                (item.name, item.read_bytes())
                for item in sorted(path.parent.iterdir())
                if item.is_file()
            ]
            lock[raw["id"]] = {
                "declaration_sha256": declaration_digest(files),
                "artifact": PluginArtifact(kind, name, version, published).to_dict(),
            }
            print(f"{raw['id']}: {reference}")

    LOCK.write_text(
        json.dumps({"format_version": 1, "plugins": lock}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"locked {len(lock)} plugins in {LOCK.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
