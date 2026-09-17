"""Write the update manifest and the checksum file a release publishes.

`latest.json` is what the updater reads from the channel endpoint: the version,
notes, publication time and, per target, the package URL and its minisign
signature. `SHA256SUMS` lists every published file, for the package managers
and people who verify by hand. Every signature is verified against the public
key before it is written into the manifest, so a manifest can only ever name
packages the builds' own key signed.

Channels: a tag `vX.Y.Z` publishes to `stable`, `vX.Y.Z-beta.N` to `beta`. The
updater never installs a lower version, so a rollback is a new, higher version
carrying the previous code - see docs/release.md.

    python scripts/release/update_manifest.py --version 0.2.0 --artifacts dist \\
        --base-url https://github.com/OWNER/REPO/releases/download/v0.2.0 \\
        --pubkey "$PROMETHEUS_UPDATER_PUBKEY" --out dist
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_minisign import SignatureError, verify

#: Updater target -> the file name pattern each build job uploads.
TARGETS = {
    "darwin-aarch64": re.compile(r"_aarch64\.app\.tar\.gz$"),
    "darwin-x86_64": re.compile(r"_x64\.app\.tar\.gz$"),
    "windows-x86_64": re.compile(r"_x64-setup\.exe$"),
    "linux-x86_64": re.compile(r"_amd64\.AppImage$"),
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-(?P<channel>[a-z]+)\.\d+)?$")


def channel_of(version: str) -> str:
    match = SEMVER.match(version)
    if not match:
        raise SystemExit(f"{version} is not a release version (X.Y.Z or X.Y.Z-beta.N)")
    return match["channel"] or "stable"


def build(version: str, artifacts: Path, base_url: str, pubkey: str, notes: str) -> dict:
    platforms: dict[str, dict[str, str]] = {}
    for target, pattern in TARGETS.items():
        found = [path for path in sorted(artifacts.rglob("*")) if pattern.search(path.name)]
        if len(found) != 1:
            raise SystemExit(f"{target}: expected one package, found {[p.name for p in found]}")
        package = found[0]
        signature = package.with_name(package.name + ".sig")
        if not signature.exists():
            raise SystemExit(f"{target}: {package.name} has no signature")
        text = signature.read_text(encoding="utf-8")
        try:
            verify(package.read_bytes(), text, pubkey)
        except SignatureError as error:
            raise SystemExit(f"{target}: {package.name}: {error}") from error
        platforms[target] = {"signature": text.strip(), "url": f"{base_url}/{package.name}"}
    return {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }


def checksums(artifacts: Path) -> str:
    lines = []
    for path in sorted(artifacts.rglob("*")):
        if path.is_file() and path.name not in {"SHA256SUMS", "latest.json"}:
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--pubkey", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()

    channel = channel_of(arguments.version)
    manifest = build(
        arguments.version,
        arguments.artifacts,
        arguments.base_url,
        arguments.pubkey,
        arguments.notes,
    )
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "latest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (arguments.out / "SHA256SUMS").write_text(checksums(arguments.artifacts), encoding="utf-8")
    print(f"{channel} manifest for {arguments.version}: {sorted(manifest['platforms'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
