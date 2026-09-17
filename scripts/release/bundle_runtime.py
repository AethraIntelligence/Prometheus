"""Build the runtime an installed application runs, for this machine's platform.

The desktop shell starts `resources/runtime/python/.../python -m app.cli.main
serve` from `resources/runtime/app-root` (`desktop/src-tauri/src/runtime.rs`).
This script produces that directory at `desktop/src-tauri/runtime-dist`:

* `python/` - a relocatable CPython (python-build-standalone, fetched and
  verified by uv), so the application depends on no Python the person has;
* the platform's dependencies installed into it from `uv.lock`, by hash, and
  nothing else - no dev tools, no project editable install;
* `app-root/` - the platform's source and the declarations it reads at run time
  (employees, prompts, workflows, plugins, validation scenarios, migrations);
* `BUILD-MANIFEST.json` - every file with its SHA-256, the lock's digest and the
  interpreter version, so what shipped can be compared with what was built.

It then starts the bundled interpreter once, with the user environment kept out,
and checks the platform is importable and the migrations are found. It builds for
the platform it runs on and refuses to cross-build: a release is built on each
supported OS's own runner, never cross-compiled.

    uv run python scripts/release/bundle_runtime.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "desktop" / "src-tauri" / "runtime-dist"
PYTHON_VERSION = "3.12"
SOURCE_PACKAGES = ("app", "application", "domain", "infrastructure")
CONTENT = ("employees", "prompts", "workflows", "plugins", "validation")
FILES = ("alembic.ini", "pyproject.toml", "uv.lock", "LICENSE")
SKIP = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}
#: Locked dependencies an installed application does not ship, and why. The
#: desktop driver pulls in two GPL-3.0+ packages, which cannot go inside an
#: MIT-licensed installer without making the whole of it GPL; without them,
#: desktop computer use in a packaged build says it is not included. Browser
#: computer use is unaffected. `scripts/release/check_licenses.py` enforces it.
NOT_BUNDLED = {
    "pyautogui": "its dependencies mouseinfo and pymsgbox are GPL-3.0-or-later",
    "mouseinfo": "GPL-3.0-or-later",
    "pymsgbox": "GPL-3.0-or-later",
}


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def interpreter_in(root: Path) -> Path:
    return root / "python.exe" if os.name == "nt" else root / "bin" / "python3"


def fetch_python(destination: Path) -> str:
    """A standalone CPython in `destination`, via uv's verified downloads."""
    with tempfile.TemporaryDirectory(prefix="prometheus-python-") as scratch:
        run(["uv", "python", "install", PYTHON_VERSION, "--install-dir", scratch, "--no-bin"])
        # uv adds a minor-version alias beside the real directory; the alias is
        # a link and the real one is what gets copied.
        installs = [
            path
            for path in Path(scratch).iterdir()
            if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
        ]
        found = [path for path in installs if interpreter_in(path).exists()]
        if len(found) != 1:
            raise SystemExit(f"expected one Python install in {scratch}, found {installs}")
        shutil.copytree(found[0], destination, symlinks=True)
    asked = "import platform; print(platform.python_version())"
    version = subprocess.run(
        [str(interpreter_in(destination)), "-c", asked],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return version


def without(requirements: str, names: dict[str, str]) -> str:
    """The exported requirements minus whole entries for `names`, hashes included."""
    kept: list[str] = []
    skipping = False
    for line in requirements.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            name = line.split("==")[0].split(";")[0].strip().lower().replace("_", "-")
            skipping = name in names
        if not skipping:
            kept.append(line)
    return "\n".join(kept) + "\n"


def install_dependencies(python: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="prometheus-requirements-") as scratch:
        requirements = Path(scratch) / "requirements.txt"
        run(
            [
                "uv", "export", "--frozen", "--no-dev", "--no-emit-project",
                "--format", "requirements.txt", "--output-file", str(requirements),
            ],
            cwd=REPO,
        )
        requirements.write_text(
            without(requirements.read_text(encoding="utf-8"), NOT_BUNDLED), encoding="utf-8"
        )
        for name, reason in NOT_BUNDLED.items():
            print(f"not bundled: {name} ({reason})")
        run(
            [
                "uv", "pip", "install", "--python", str(python), "--require-hashes",
                "--no-deps", "--break-system-packages", "-r", str(requirements),
            ],
            cwd=REPO,
        )


def copy_source(app_root: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in SKIP or name.endswith((".pyc", ".db"))}

    for name in (*SOURCE_PACKAGES, *CONTENT):
        shutil.copytree(REPO / name, app_root / name, ignore=ignore)
    for name in FILES:
        shutil.copy2(REPO / name, app_root / name)


def smoke(python: Path, app_root: Path) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PROMETHEUS_"))
    }
    with tempfile.TemporaryDirectory(prefix="prometheus-bundle-smoke-") as data:
        environment.update(
            {
                "PYTHONPATH": str(app_root),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PROMETHEUS_DATA_DIR": data,
                "PROMETHEUS_SECRET_BACKEND": "file",
            }
        )
        for arguments in (["--version"], ["migrate", "--check"], ["migrate"]):
            run([str(python), "-m", "app.cli.main", *arguments], cwd=app_root, env=environment)


def manifest(out: Path, python_version: str) -> None:
    files = {
        path.relative_to(out).as_posix(): sha256(path)
        for path in sorted(out.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    (out / "BUILD-MANIFEST.json").write_text(
        json.dumps(
            {
                "platform": {"system": platform.system(), "machine": platform.machine()},
                "python": python_version,
                "lock_sha256": sha256(REPO / "uv.lock"),
                "files": files,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    arguments = parser.parse_args()
    out: Path = arguments.out.resolve()

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    python_root = out / "python"
    version = fetch_python(python_root)
    python = interpreter_in(python_root)
    install_dependencies(python)
    copy_source(out / "app-root")
    smoke(python, out / "app-root")
    manifest(out, version)
    print(f"runtime bundled at {out} (Python {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
