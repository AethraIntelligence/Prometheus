"""Fill `tauri.release.conf.json` from the release environment, refusing gaps.

The template names the update channel's endpoint and the updater public key as
`${VARIABLE}`. A release built with either missing would ship an updater that
trusts nothing or checks nowhere, so a missing value stops the build here, by
name. Windows signing adds the certificate thumbprint when the runner has one.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

TAURI = Path(__file__).resolve().parents[2] / "desktop" / "src-tauri"
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def main() -> int:
    template = (TAURI / "tauri.release.conf.template.json").read_text(encoding="utf-8")
    missing = sorted({name for name in PLACEHOLDER.findall(template) if not os.environ.get(name)})
    if missing:
        print(f"refusing to build a release without: {', '.join(missing)}", file=sys.stderr)
        return 1
    rendered = json.loads(PLACEHOLDER.sub(lambda match: os.environ[match.group(1)], template))
    thumbprint = os.environ.get("WINDOWS_CERTIFICATE_THUMBPRINT")
    if thumbprint:
        rendered["bundle"]["windows"] = {
            "certificateThumbprint": thumbprint,
            "digestAlgorithm": "sha256",
            "timestampUrl": os.environ.get("WINDOWS_TIMESTAMP_URL", "http://timestamp.digicert.com"),
        }
    (TAURI / "tauri.release.conf.json").write_text(json.dumps(rendered, indent=2), encoding="utf-8")
    print("tauri.release.conf.json written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
