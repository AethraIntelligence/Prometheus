"""The facts `domain.safety.platform.check` needs, read from this machine."""

from __future__ import annotations

import platform

from domain.safety.platform import PlatformVerdict, check


def this_machine() -> tuple[str, str, str]:
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0]
    elif system == "Windows":
        release = platform.version()
    elif system == "Linux":
        library, version = platform.libc_ver()
        release = version if library == "glibc" else ""
    else:
        release = platform.release()
    return system, release, platform.machine()


def verdict() -> PlatformVerdict:
    return check(*this_machine())
