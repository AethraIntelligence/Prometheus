"""Phase 13: what the release workflow checks before it publishes anything.

The signature format is minisign as Tauri's signer writes it (checked once by hand
against `tauri signer sign`; see the Phase 13 record); these tests build keys and
signatures in that format so the suite needs neither the Tauri CLI nor a network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "release"))

import update_manifest
from verify_minisign import SignatureError, verify

KEY_ID = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    text = "untrusted comment: minisign public key\n" + base64.b64encode(
        b"Ed" + KEY_ID + raw
    ).decode()
    return private, base64.b64encode(text.encode()).decode()


def sign(private: Ed25519PrivateKey, content: bytes, *, key_id: bytes = KEY_ID) -> str:
    signature = private.sign(hashlib.blake2b(content, digest_size=64).digest())
    trusted = "timestamp:1\tfile:package"
    global_signature = private.sign(signature + trusted.encode())
    text = "\n".join(
        [
            "untrusted comment: signature from tauri secret key",
            base64.b64encode(b"ED" + key_id + signature).decode(),
            f"trusted comment: {trusted}",
            base64.b64encode(global_signature).decode(),
        ]
    )
    return base64.b64encode(text.encode()).decode()


def test_a_genuine_package_verifies() -> None:
    private, public = keypair()

    assert verify(b"package", sign(private, b"package"), public) == "timestamp:1\tfile:package"


@pytest.mark.parametrize(
    "case",
    ["changed after signing", "truncated", "another key", "another key id"],
)
def test_anything_but_the_signed_package_under_the_embedded_key_is_refused(case: str) -> None:
    private, public = keypair()
    content = b"the package as built" * 100
    signature = sign(private, content)
    if case == "changed after signing":
        content = content.replace(b"built", b"swapd")
    elif case == "truncated":
        content = content[: len(content) // 2]
    elif case == "another key":
        signature = sign(Ed25519PrivateKey.generate(), content)
    else:
        signature = sign(private, content, key_id=b"\xff" * 8)

    with pytest.raises(SignatureError):
        verify(content, signature, public)


PACKAGES = {
    "darwin-aarch64": "Prometheus_0.2.0_aarch64.app.tar.gz",
    "darwin-x86_64": "Prometheus_0.2.0_x64.app.tar.gz",
    "windows-x86_64": "Prometheus_0.2.0_x64-setup.exe",
    "linux-x86_64": "prometheus_0.2.0_amd64.AppImage",
}


def artifacts(tmp_path: Path, private: Ed25519PrivateKey) -> Path:
    directory = tmp_path / "dist"
    for target, name in PACKAGES.items():
        folder = directory / target
        folder.mkdir(parents=True)
        content = os.urandom(64)
        (folder / name).write_bytes(content)
        (folder / f"{name}.sig").write_text(sign(private, content), encoding="utf-8")
    return directory


def test_the_manifest_names_every_target_with_a_verified_signature(tmp_path: Path) -> None:
    private, public = keypair()
    directory = artifacts(tmp_path, private)

    base = "https://example.invalid/v0.2.0"
    manifest = update_manifest.build("0.2.0", directory, base, public, "")

    assert sorted(manifest["platforms"]) == sorted(PACKAGES)
    assert manifest["platforms"]["linux-x86_64"]["url"].endswith("_amd64.AppImage")
    sums = update_manifest.checksums(directory)
    assert len(sums.strip().splitlines()) == 8
    json.dumps(manifest)


def test_a_manifest_is_never_written_for_a_package_the_key_did_not_sign(tmp_path: Path) -> None:
    private, public = keypair()
    directory = artifacts(tmp_path, private)
    tampered = directory / "windows-x86_64" / PACKAGES["windows-x86_64"]
    tampered.write_bytes(b"replaced after signing")

    with pytest.raises(SystemExit, match="windows-x86_64"):
        update_manifest.build("0.2.0", directory, "https://example.invalid", public, "")


def test_a_package_without_a_signature_stops_the_release(tmp_path: Path) -> None:
    private, public = keypair()
    directory = artifacts(tmp_path, private)
    (directory / "darwin-x86_64" / f"{PACKAGES['darwin-x86_64']}.sig").unlink()

    with pytest.raises(SystemExit, match="no signature"):
        update_manifest.build("0.2.0", directory, "https://example.invalid", public, "")


@pytest.mark.parametrize(
    ("version", "channel"),
    [("1.2.3", "stable"), ("1.2.3-beta.4", "beta"), ("1.2.3-rc.1", "rc")],
)
def test_the_channel_follows_the_version(version: str, channel: str) -> None:
    assert update_manifest.channel_of(version) == channel


def test_a_version_that_is_not_a_release_is_refused() -> None:
    with pytest.raises(SystemExit):
        update_manifest.channel_of("latest")


def test_a_release_config_is_not_rendered_with_a_missing_key(monkeypatch, capsys) -> None:
    import render_tauri_config

    monkeypatch.delenv("PROMETHEUS_UPDATER_PUBKEY", raising=False)
    monkeypatch.setenv("PROMETHEUS_UPDATE_ENDPOINT", "https://example.invalid/latest.json")

    assert render_tauri_config.main() == 1
    assert "PROMETHEUS_UPDATER_PUBKEY" in capsys.readouterr().err


def test_the_scripted_model_answers_each_kind_of_drill_request() -> None:
    import scripted_model

    tools = [{"type": "function", "function": {"name": "fs_write", "parameters": {}}}]
    call = scripted_model.answer({"messages": [{"role": "user", "content": "go"}], "tools": tools})
    assert call["tool_calls"][0]["function"]["name"] == "fs_write"
    done = scripted_model.answer(
        {"messages": [{"role": "tool", "content": "ok"}], "tools": tools}
    )
    assert "tool_calls" not in done
    asked = [{"role": "system", "content": 'reply {"steps": []}'}]
    plan = scripted_model.answer({"messages": asked})
    assert "steps" in json.loads(plan["content"])
