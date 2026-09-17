# Releasing Prometheus

How an installable release is built, what it is checked against before anyone can
download it, and what an installed application does when it is stopped, upgraded,
backed up and restored. The decision behind all of it is
[ADR 0030](adr/0030-an-installation-can-be-stopped-moved-upgraded-and-put-back.md).

## Supported systems

| OS | Versions | Architectures | Packages |
|---|---|---|---|
| macOS | 13 Ventura and newer | Apple silicon (arm64), Intel (x86_64) | `.dmg`; updates as `.app.tar.gz` |
| Windows | 10 22H2 (build 19045) and 11 | x86_64 | NSIS `-setup.exe` |
| Linux | Ubuntu 22.04 / 24.04, Debian 12, Fedora 40+ (glibc 2.35+, WebKitGTK 4.1) | x86_64 | `.deb`, `.rpm`, `.AppImage` |

Minimum: 8 GB of memory and 2 GB of free disk for the application and its data.
WebView2 on Windows (the installer fetches it). Docker is optional: without it,
running generated code is switched off rather than run unsandboxed.

Anything else is unsupported and is told so before anything is installed or
created: the `.dmg` declares the minimum macOS version, the Windows installer
refuses older builds and non-x64 machines, the Linux packages depend on the
WebKitGTK they need, and the runtime itself refuses to start on an unsupported
system (`domain/safety/platform.py`).

## Versions, channels and rollback

- A tag `vX.Y.Z` releases to the **stable** channel; `vX.Y.Z-beta.N` to **beta**,
  published as a GitHub pre-release so the stable channel's
  `releases/latest/download/latest.json` never serves it.
- `desktop/src-tauri/tauri.conf.json` must carry the same version as the tag; the
  workflow refuses a mismatch.
- The updater never installs a lower version. **A rollback is a new, higher
  patch version carrying the previous code**, released the normal way. The
  database is not downgraded: a newer schema is refused by an older build, and
  the pre-migration backup in `backups/` is how a person returns to the older
  version by hand (`prometheus restore`).
- Every release is created as a draft. A person reads the drill evidence for each
  OS and publishes it.

## Signing identities

The workflow refuses to build a release without all of them. They are the
repository owner's and are stored as GitHub secrets, never in the repository.

| Secret | What it is |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | the updater signing key (`npx tauri signer generate`) |
| `PROMETHEUS_UPDATER_PUBKEY` | its public key, compiled into every build and used to verify before publishing |
| `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY` | the Developer ID Application certificate (base64 `.p12`) |
| `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` | notarization (an app-specific password) |
| `WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD` | the Authenticode code-signing certificate (base64 `.pfx`) |

Losing the updater key means existing installations can no longer be updated
automatically; keep it backed up offline.

## What the release workflow does

`.github/workflows/release.yml`, on a tag:

1. **Preflight** - version and channel, the version in `tauri.conf.json`, every
   signing identity present.
2. **Checks** - the CI workflow: lint, architecture contracts, English-only,
   types against `typing-baseline.json`, the whole suite with coverage floors
   per critical area, property tests, the validation release contract, the
   window's tests and build, the shell's format, lints and tests, and
   vulnerability scans of Python (by hash), npm (production) and Rust.
3. **SBOM** - CycloneDX for the Python runtime, the window and the shell.
4. **Build, on each OS's own runner** (never cross-compiled):
   - `scripts/release/bundle_runtime.py` builds a relocatable CPython with the
     locked dependencies installed by hash and the platform's source;
   - `scripts/release/check_licenses.py` refuses anything the installer may not
     ship;
   - `scripts/release/render_tauri_config.py` fills the update endpoint and key;
   - Tauri builds, signs and (on macOS) notarizes;
   - every updater signature is verified, and a corrupted and a truncated copy of
     each package must be refused; macOS `codesign`, `spctl` and `stapler`, and
     Windows Authenticode, must pass;
   - the package is **installed** and `scripts/release/drill.py` drives the
     installed application: first run, a model, a task with an approval and an
     artifact, a graceful restart, a forced crash and recovery, the update
     safe-point and restart, a backup, deleting the local data, a restore, the
     result seen again, and the emergency stop. The evidence is kept as a
     workflow artifact.
5. **Publish** - `scripts/release/update_manifest.py` writes `latest.json` from
   verified signatures only and `SHA256SUMS` over every file; a draft release
   holds the packages, the manifest, the checksums and the SBOMs.

The drill runs anywhere the runtime can be started, with no provider key - a
scripted model answers on loopback:

```bash
uv run python scripts/release/drill.py --launch "uv run prometheus serve" --evidence drill.json
```

## What ships, and what does not

- The runtime is a relocatable CPython 3.12 with the dependencies from `uv.lock`,
  checked by hash, plus the platform's source, declarations and migrations.
  `BUILD-MANIFEST.json` in the bundle lists every file with its SHA-256.
- **Desktop computer use is not in the packaged application.** Its driver
  (`pyautogui`) depends on `mouseinfo` and `pymsgbox`, which are GPL-3.0-or-later;
  shipping them would place the whole installer under the GPL. From a source
  checkout it works as before. Computer use inside the browser is unaffected.
- Plugins run pinned packages whose published digests are locked in
  `plugins/catalog.lock.json`. To change a plugin or move to newer versions, run
  `uv run python scripts/release/lock_plugins.py [--upgrade]` and review the diff.
  Their own dependencies are resolved by their package managers at run time.

## Operating an installation

**Emergency stop.** The red Stop button on every screen, *Work -> Stop All Work*
(Cmd/Ctrl+Shift+.) in the application menu, or `prometheus stop` from any
terminal. It stops all work until somebody resumes it, including across restarts.
Nothing that already happened is undone. `prometheus stop --clear` or *Resume
work* lifts it; nothing stopped starts again on its own.

**One engine per data directory.** Opening the application twice focuses the
first window. A second `prometheus serve`, or a CLI command that runs work, is
refused while an engine owns the data directory and names it (exit code 3).

**Upgrades.** On start, the runtime checks the database's schema. A database from
a newer version is refused without being changed. An older one is upgraded after
a verified backup into `backups/`; a failed or interrupted upgrade is undone from
that backup. An update is installed only when no irreversible action is in
flight; work in progress resumes after the restart.

**Secrets.** The key that encrypts stored credentials lives in the system's
credential vault. A server sets `PROMETHEUS_MASTER_KEY`; a headless machine that
wants a file says so with `PROMETHEUS_SECRET_BACKEND=file`.

**Backup and restore.** Settings -> Backups and updates, or:

```bash
prometheus backup                     # to ~/Prometheus Backups
prometheus backup --with-secrets      # adds the key, sealed with a passphrase
prometheus restore BACKUP.zip --check # verify only
prometheus restore BACKUP.zip         # with no runtime running
```

A backup is never synchronisation: it is made when asked and read only when
restored. A restore checks the whole archive first, keeps the installation it
replaces beside it, and never overwrites a file that changed since the backup.
