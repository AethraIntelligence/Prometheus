"""Verify a minisign signature - what the updater checks, checked before publishing.

Tauri's updater signs each update package with a minisign key and verifies the
signature on the person's machine before replacing anything. The release
workflow verifies the same signatures with the public key the builds embed, on
the runner, before a single file is published: a package signed with the wrong
key, or changed after signing, never reaches the update channel.

Both minisign algorithms are accepted - `Ed` (the file signed directly) and
`ED` (a BLAKE2b-512 prehash, which is what Tauri's signer produces) - and the
trusted comment is verified with the global signature, as minisign does.

    python scripts/release/verify_minisign.py FILE FILE.sig --pubkey BASE64
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class SignatureError(Exception):
    pass


def _lines(text: str) -> list[str]:
    return [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def _maybe_base64_wrapped(text: str) -> str:
    """Tauri stores both the key and the signature as base64 of the minisign file."""
    stripped = text.strip()
    if stripped.startswith("untrusted comment:"):
        return stripped
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise SignatureError("not a minisign file, raw or base64-wrapped") from error
    return decoded


def public_key(text: str) -> tuple[bytes, Ed25519PublicKey]:
    lines = _lines(_maybe_base64_wrapped(text))
    raw = base64.b64decode(lines[-1])
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise SignatureError("not an Ed25519 minisign public key")
    return raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:])


def verify(content: bytes, signature_text: str, key_text: str) -> str:
    """The trusted comment, if the signature is valid; `SignatureError` otherwise."""
    key_id, key = public_key(key_text)
    lines = _lines(_maybe_base64_wrapped(signature_text))
    if len(lines) != 4 or not lines[2].startswith("trusted comment: "):
        raise SignatureError("malformed signature file")
    raw = base64.b64decode(lines[1])
    if len(raw) != 74:
        raise SignatureError("malformed signature")
    algorithm, signed_by, signature = raw[:2], raw[2:10], raw[10:]
    if signed_by != key_id:
        raise SignatureError("signed with a different key")
    if algorithm == b"ED":
        message = hashlib.blake2b(content, digest_size=64).digest()
    elif algorithm == b"Ed":
        message = content
    else:
        raise SignatureError("unknown signature algorithm")
    trusted = lines[2][len("trusted comment: ") :]
    global_signature = base64.b64decode(lines[3])
    try:
        key.verify(signature, message)
        key.verify(global_signature, signature + trusted.encode("utf-8"))
    except InvalidSignature as error:
        raise SignatureError("the signature does not match the file") from error
    return trusted


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("file", type=Path)
    parser.add_argument("signature", type=Path)
    parser.add_argument("--pubkey", required=True, help="The key as tauri.conf carries it.")
    arguments = parser.parse_args()
    try:
        trusted = verify(
            arguments.file.read_bytes(),
            arguments.signature.read_text(encoding="utf-8"),
            arguments.pubkey,
        )
    except SignatureError as error:
        print(f"INVALID {arguments.file}: {error}", file=sys.stderr)
        return 1
    print(f"valid {arguments.file} ({trusted})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
