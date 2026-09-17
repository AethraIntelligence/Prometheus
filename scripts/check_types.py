"""Type checking with a baseline that may only shrink.

The code base predates type checking, and switching it on found more than a
hundred errors - most of them stub gaps, some of them real. Fixing all of them at
once would bury the real ones in noise; ignoring the checker would miss the next
real one. So the errors that existed are counted per file and error code in
`typing-baseline.json`, and this fails when any count grows or a new
(file, code) appears. A count that shrinks is reported so the baseline can be
lowered in the same change.

    uv run python scripts/check_types.py            # the check CI runs
    uv run python scripts/check_types.py --update   # only after removing errors
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "typing-baseline.json"
LINE = re.compile(r"^(?P<file>[^:]+):\d+: error: .*\[(?P<code>[a-z0-9-]+)\]$")


def current() -> Counter[str]:
    finished = subprocess.run(
        ["mypy", "--no-error-summary", "--no-color-output"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode not in (0, 1):
        sys.stderr.write(finished.stdout + finished.stderr)
        raise SystemExit("mypy itself failed")
    counts: Counter[str] = Counter()
    for line in finished.stdout.splitlines():
        match = LINE.match(line.strip())
        if match:
            counts[f"{Path(match['file']).as_posix()}::{match['code']}"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    arguments = parser.parse_args()
    found = current()
    recorded: dict[str, int] = (
        json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    )

    if arguments.update:
        grown = {key: count for key, count in found.items() if count > recorded.get(key, 0)}
        if recorded and grown:
            print("Refusing to record new errors in the baseline:", file=sys.stderr)
            for key, count in sorted(grown.items()):
                print(f"  {key}: {recorded.get(key, 0)} -> {count}", file=sys.stderr)
            return 1
        BASELINE.write_text(json.dumps(dict(sorted(found.items())), indent=2) + "\n", "utf-8")
        print(f"baseline: {sum(found.values())} error(s) in {len(found)} file/code pair(s)")
        return 0

    new = {key: count for key, count in found.items() if count > recorded.get(key, 0)}
    fewer = {key: count for key, count in recorded.items() if found.get(key, 0) < count}
    for key, count in sorted(new.items()):
        print(f"new type error(s): {key}: {recorded.get(key, 0)} -> {count}", file=sys.stderr)
    if fewer:
        print(
            f"{len(fewer)} baseline entr(ies) improved; lower them with --update.",
            file=sys.stderr,
        )
    print(f"type errors: {sum(found.values())} (baseline {sum(recorded.values())})")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())
