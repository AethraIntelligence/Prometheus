"""The license gate: what an installed application ships must be licensed to ship.

Reads the licenses of every package installed into the bundled runtime (from the
packages' own metadata) and fails on anything outside the allow-list. Copyleft
that would reach the MIT-licensed application as a whole is not allowed; a
package that needs it is excluded from the bundle (`bundle_runtime.NOT_BUNDLED`)
rather than waved through here. A license the metadata does not state is a
failure too, with the package named, so a person looks at it.

    python scripts/release/check_licenses.py desktop/src-tauri/runtime-dist/python/bin/python3
"""

from __future__ import annotations

import json
import subprocess
import sys

#: Normalised license words that may ship inside the installer.
ALLOWED = (
    "MIT",
    "BSD",
    "APACHE",
    "ISC",
    "PSF",
    "PYTHON SOFTWARE FOUNDATION",
    "MPL",  # file-level copyleft; shipped unmodified
    "MOZILLA PUBLIC LICENSE",
    "UNLICENSE",
    "0BSD",
    "ZLIB",
    "HPND",
    "LGPL",  # installed as separate, replaceable packages
    "LESSER GENERAL PUBLIC",
)
DENIED = (
    "AGPL", "AFFERO", "GNU GENERAL PUBLIC LICENSE", "GPL-2", "GPL-3", "GPLV2", "GPLV3", "SSPL",
)
#: Metadata that names no license although the package is permissively licensed,
#: reviewed by hand. Each entry says what the license actually is.
REVIEWED = {
    "pywin32": "PSF-2.0",
}

PROBE = r"""
import json
from importlib.metadata import distributions
found = []
for dist in distributions():
    meta = dist.metadata
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    found.append({
        "name": meta["Name"],
        "version": meta["Version"],
        "expression": meta.get("License-Expression") or "",
        "license": (meta.get("License") or "")[:200],
        "classifiers": classifiers,
    })
print(json.dumps(found))
"""


def verdict(package: dict) -> tuple[bool, str]:
    name = str(package["name"]).lower()
    if name in REVIEWED:
        return True, REVIEWED[name]
    stated = " ".join(
        [package["expression"], package["license"], *package["classifiers"]]
    ).upper()
    lgpl_only = "LGPL" in stated or "LESSER GENERAL PUBLIC" in stated
    if any(word in stated for word in DENIED) and not lgpl_only:
        return False, stated.strip()
    if any(word in stated for word in ALLOWED):
        return True, stated.strip()
    return False, stated.strip() or "no license stated"


def main() -> int:
    python = sys.argv[1] if len(sys.argv) > 1 else sys.executable
    raw = subprocess.run(
        [python, "-I", "-c", PROBE], check=True, capture_output=True, text=True
    ).stdout
    failures = []
    for package in sorted(json.loads(raw), key=lambda item: str(item["name"]).lower()):
        allowed, stated = verdict(package)
        if not allowed:
            failures.append(f"{package['name']} {package['version']}: {stated}")
    for failure in failures:
        print(f"not allowed to ship: {failure}", file=sys.stderr)
    print(f"licenses checked: {len(json.loads(raw))} package(s), {len(failures)} refused")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
