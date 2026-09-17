"""Which machines this release is for, stated once and checked before anything runs.

The matrix is a product decision recorded in the Phase 13 roadmap entry and in
`docs/release.md`; this module is the same decision as data, so the installer's
minimum versions, the release workflow's runners and the runtime's refusal
cannot drift from one another without a test noticing.

An unsupported machine is told so, plainly, before it creates a data directory.
It is not a silent fallback and not a warning buried in a log: an application
that half-works on a system nobody tested is the support case this prevents.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformSupport:
    system: str
    architectures: frozenset[str]
    minimum: tuple[int, ...]
    minimum_label: str
    packages: tuple[str, ...]


#: `system` is `platform.system()`; architectures are normalised names.
SUPPORTED: tuple[PlatformSupport, ...] = (
    PlatformSupport("Darwin", frozenset({"arm64", "x86_64"}), (13,), "macOS 13 Ventura", ("dmg",)),
    PlatformSupport("Windows", frozenset({"x86_64"}), (10, 0, 19045), "Windows 10 22H2", ("nsis",)),
    PlatformSupport(
        "Linux", frozenset({"x86_64"}), (2, 35), "glibc 2.35", ("deb", "rpm", "appimage")
    ),
)

_ARCHITECTURES = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


@dataclass(frozen=True, slots=True)
class PlatformVerdict:
    supported: bool
    message: str = ""


def _version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.replace("-", ".").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check(system: str, release: str, machine: str) -> PlatformVerdict:
    """`release` is the OS version for macOS and Windows, the glibc version for Linux."""
    architecture = _ARCHITECTURES.get(machine.lower(), machine.lower())
    for entry in SUPPORTED:
        if entry.system != system:
            continue
        if architecture not in entry.architectures:
            return PlatformVerdict(
                False,
                f"Prometheus does not support {system} on {architecture}. Supported: "
                f"{', '.join(sorted(entry.architectures))}.",
            )
        found = _version(release)
        if not found or found < entry.minimum:
            return PlatformVerdict(
                False,
                f"Prometheus needs {entry.minimum_label} or newer; this machine reports "
                f"{release or 'an unknown version'}.",
            )
        return PlatformVerdict(True)
    return PlatformVerdict(
        False,
        f"Prometheus does not support {system or 'this operating system'}. Supported: "
        "macOS 13+, Windows 10 22H2+ and Linux with glibc 2.35+.",
    )
